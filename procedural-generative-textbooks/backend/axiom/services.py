"""Application services shared by the API routers (built once at startup).

:class:`AppServices` owns the process-wide pieces (settings, the app DB, the benchmark pool,
the skill graph, locks) and hands out per-request objects (an :class:`LLMClient` that re-reads
the LLM settings from the app DB every time so a key change applies without a restart, a
:class:`Pedagogy` around it, sandboxes bound to one case). :class:`WorldService` generates,
loads, projects and updates case worlds.

Module 2 (``axiom.engine.procedural_world`` / ``axiom.engine.archetypes``) is imported lazily so
this module imports even while Module 2 is absent.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import shutil
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from axiom.api.public_view import (
    build_asks,
    normalize_database,
    public_case_data,
    public_levers,
)
from axiom.api.schemas import Ask, CasePublicView, CaseStatus, WorldStats
from axiom.config import Settings, get_settings
from axiom.engine.llm_client import LLMClient, LLMConfigError
from axiom.engine.pedagogy import Pedagogy
from axiom.engine.rng import SeededRNG
from axiom.engine.sandbox import PythonSandbox, SQLSandbox
from axiom.models import (
    ArchetypeInfo,
    CaseRecord,
    CaseWorld,
    DatabaseSpec,
    GenerateCaseRequest,
    LLMSettings,
    SimulationState,
)
from axiom.persistence import Persistence, utc_now_iso
from axiom.skill_graph import SkillGraph

if TYPE_CHECKING:
    from axiom.engine.market_research import BenchmarkPool

__all__ = [
    "AppServices",
    "WorldService",
    "ModuleUnavailableError",
    "CaseNotFoundError",
    "LLM_NOT_CONFIGURED",
    "load_archetype_catalog",
    "load_generate_world",
]

logger = logging.getLogger("axiom.services")

LLM_NOT_CONFIGURED = (
    "The LLM is not configured. Open Settings, choose a provider and model, and add an API key "
    "(or choose the local provider), then try again."
)
_VALID_STATUS: tuple[CaseStatus, ...] = ("generated", "briefed", "submitted", "graded")


class ModuleUnavailableError(RuntimeError):
    """A module this feature needs (Module 2, the world generator) is not installed yet."""


class CaseNotFoundError(KeyError):
    """No case with that id (maps to HTTP 404)."""

    def __str__(self) -> str:  # KeyError quotes its argument; keep the message readable
        return str(self.args[0]) if self.args else "case not found"


def load_generate_world() -> Callable[..., CaseWorld]:
    """Import ``generate_world`` from Module 2 or raise :class:`ModuleUnavailableError`."""
    try:
        from axiom.engine.procedural_world import generate_world
    except (ImportError, SyntaxError) as exc:
        raise ModuleUnavailableError(f"the world generator (Module 2) is not available: {exc}") from exc
    return generate_world


def load_archetype_catalog() -> list[ArchetypeInfo]:
    """``axiom.engine.archetypes.catalog()``, or ``[]`` while Module 2 is absent or broken."""
    try:
        from axiom.engine.archetypes import catalog

        return list(catalog())
    except ImportError as exc:
        logger.info("archetype catalog unavailable: %s", exc)
    except Exception:  # a half-installed Module 2 must not take the API down
        logger.exception("archetype catalog failed to load")
    return []


# --------------------------------------------------------------------------- world service


class WorldService:
    """Generate, load, project and update case worlds (``world.json`` + ``company.duckdb``)."""

    def __init__(self, services: AppServices) -> None:
        self.services = services
        self._cache: dict[str, tuple[float, CaseWorld]] = {}
        self._cache_lock = threading.Lock()

    # ------------------------------------------------------------------ paths

    @property
    def cases_dir(self) -> Path:
        """Root folder of all case folders."""
        assert self.services.settings.cases_dir is not None
        return Path(self.services.settings.cases_dir)

    def record(self, case_id: str) -> CaseRecord:
        """The app-DB row of a case (raises :class:`CaseNotFoundError`)."""
        rec = self.services.persistence.get_case(case_id)
        if rec is None:
            raise CaseNotFoundError(f"case {case_id!r} not found")
        return rec

    def world_path(self, case_id: str) -> Path:
        """Path of ``world.json`` for a case."""
        rec = self.services.persistence.get_case(case_id)
        if rec is not None and rec.world_path:
            return Path(rec.world_path)
        return self.cases_dir / case_id / "world.json"

    def db_path(self, case_id: str) -> Path:
        """Path of ``company.duckdb`` for a case."""
        rec = self.record(case_id)
        if rec.db_path and Path(rec.db_path).is_absolute():
            return Path(rec.db_path)
        return Path(rec.world_path).parent / "company.duckdb" if rec.world_path else self.cases_dir / case_id / "company.duckdb"

    def case_dir(self, case_id: str) -> Path:
        """Folder holding a case's files."""
        return self.db_path(case_id).parent

    # ------------------------------------------------------------------ generate

    def _select(self, req: GenerateCaseRequest, seed: int) -> tuple[str, str | None, list[str]]:
        """Pick ``(archetype, difficulty, skill_nodes)`` via the skill graph."""
        catalog = load_archetype_catalog()
        if not catalog:
            raise ModuleUnavailableError("no archetypes are registered (Module 2 not available)")
        recent = [c.archetype for c in reversed(self.services.persistence.list_cases(limit=3))]
        with self.services.graph_lock:
            choice = self.services.graph.select_case(
                catalog, SeededRNG(seed, name="case_select"), difficulty=req.difficulty,
                target_skills=req.target_skills or None, recent_cases=recent,
            )
        logger.info("case selection: %s", choice["rationale"])
        return str(choice["archetype"]), choice.get("difficulty"), list(choice.get("skill_nodes") or [])

    def _place_files(self, world: CaseWorld, staging: Path) -> tuple[Path, Path]:
        """Move the generator's output into ``cases_dir/<case_id>/``; return (world_path, db_path).

        Accepts either layout for ``out_dir`` (the case folder itself, or a parent holding
        ``<case_id>/``) and the generator's default location as a fallback.
        """
        final = self.cases_dir / world.case_id
        candidates = [staging, staging / world.case_id, Path(get_settings().cases_dir or "") / world.case_id]
        db_src = next((c / "company.duckdb" for c in candidates if (c / "company.duckdb").is_file()), None)
        if db_src is None:
            rel = Path(world.database.path)
            for base in (self.services.settings.data_dir, get_settings().data_dir):
                if (Path(base) / rel).is_file():
                    db_src = Path(base) / rel
                    break
        if db_src is None:
            raise RuntimeError(f"the generator did not write company.duckdb for {world.case_id}")
        src_dir = db_src.parent
        if src_dir.resolve() != final.resolve():
            if final.exists():
                shutil.rmtree(final)
            final.mkdir(parents=True, exist_ok=True)
            for item in src_dir.iterdir():
                if item.is_file():
                    shutil.move(str(item), str(final / item.name))
        db_final = final / "company.duckdb"
        try:
            world.database.path = db_final.relative_to(self.services.settings.data_dir).as_posix()
        except ValueError:
            world.database.path = str(db_final)
        world_final = final / "world.json"
        self._write_world(world, world_final)
        return world_final, db_final

    def generate(self, req: GenerateCaseRequest) -> CasePublicView:
        """Generate a case (section 5): the skill graph picks the archetype when omitted; the
        seed defaults to ``secrets.randbits(31)``. Persists a :class:`CaseRecord`."""
        generate_world = load_generate_world()
        seed = int(req.seed) if req.seed is not None else secrets.randbits(31)
        archetype, difficulty = req.archetype, req.difficulty
        if not archetype:
            archetype, picked, _nodes = self._select(req, seed)
            difficulty = difficulty or picked  # type: ignore[assignment]
        difficulty = difficulty or "practitioner"
        staging = self.cases_dir / f"_staging_{uuid.uuid4().hex[:10]}"
        staging.mkdir(parents=True, exist_ok=True)
        try:
            world = generate_world(archetype, seed, difficulty=difficulty, out_dir=staging, pool=self.services.pool)
            with self.services.case_lock(world.case_id):
                world_path, db_path = self._place_files(world, staging)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        record = CaseRecord(
            case_id=world.case_id, seed=world.seed, archetype=world.archetype, difficulty=world.difficulty,
            company_name=world.company.name, industry=world.industry.code, pillars=list(world.pillars),
            skill_nodes=list(world.skill_nodes), world_path=str(world_path), db_path=str(db_path),
            status="generated", created_at=utc_now_iso(),
        )
        self.services.persistence.upsert_case(record)
        self.services.persistence.set_case_status(world.case_id, "generated")
        with self._cache_lock:
            self._cache.pop(world.case_id, None)
        logger.info("case generated: %s archetype=%s seed=%d difficulty=%s", world.case_id, archetype, seed, difficulty)
        return self.public(world)

    # ------------------------------------------------------------------ load / save

    def load(self, case_id: str) -> CaseWorld:
        """Load a case world (cached by file mtime). Raises :class:`CaseNotFoundError`."""
        path = self.world_path(case_id)
        try:
            mtime = path.stat().st_mtime_ns
        except OSError:
            raise CaseNotFoundError(f"case {case_id!r} not found") from None
        with self._cache_lock:
            hit = self._cache.get(case_id)
            if hit is not None and hit[0] == mtime:
                return hit[1].model_copy(deep=True)
        world = CaseWorld.model_validate_json(path.read_text(encoding="utf-8"))
        with self._cache_lock:
            self._cache[case_id] = (mtime, world)
        return world.model_copy(deep=True)

    @staticmethod
    def _write_world(world: CaseWorld, path: Path) -> None:
        """Atomic write of ``world.json``."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
        tmp.write_text(world.model_dump_json(indent=None), encoding="utf-8")
        os.replace(tmp, path)

    def save_simulation(self, case_id: str, simulation: SimulationState) -> None:
        """Write ``simulation`` back into the case's ``world.json`` (per-case lock)."""
        with self.services.case_lock(case_id):
            world = self.load(case_id)
            world.simulation = simulation
            path = self.world_path(case_id)
            self._write_world(world, path)
            with self._cache_lock:
                self._cache.pop(case_id, None)

    # ------------------------------------------------------------------ public projection

    @staticmethod
    def asks(world: CaseWorld) -> list[Ask]:
        """Section 11 asks: key, label, unit and hint per expected metric (no value/derivation)."""
        return build_asks(world)

    def status(self, case_id: str) -> CaseStatus:
        """Case status from the app DB (``generated`` when unknown)."""
        rec = self.services.persistence.get_case(case_id)
        value = rec.status if rec is not None else "generated"
        return value if value in _VALID_STATUS else "generated"  # type: ignore[return-value]

    def public(self, world: CaseWorld) -> CasePublicView:
        """The public case view plus asks, levers and status: no ground truth and no concealed
        facts (see :func:`axiom.api.public_view.public_case_data` for every stripped path)."""
        data: dict[str, Any] = public_case_data(world)
        data["asks"] = [a.model_dump() for a in self.asks(world)]
        data["levers"] = public_levers(world)
        data["status"] = self.status(world.case_id)
        return CasePublicView.model_validate(data)

    def schema(self, case_id: str) -> DatabaseSpec:
        """The case's ``DatabaseSpec`` with sample-row timestamps in ISO 8601 (as ``/sandbox/sql``)."""
        world = self.load(case_id)
        return DatabaseSpec.model_validate(normalize_database(world.database.model_dump(mode="json")))

    def world_stats(self, case_id: str) -> WorldStats:
        """Table/row counts, periods, invariant results and generation time of a case."""
        world = self.load(case_id)
        checks = world.finance.invariants
        generation_ms: float | None = None
        meta = self.world_path(case_id).parent / "meta.json"
        try:
            seconds = json.loads(meta.read_text(encoding="utf-8")).get("build_seconds")
            generation_ms = round(float(seconds) * 1000, 1) if seconds is not None else None
        except (OSError, ValueError, TypeError, AttributeError):
            generation_ms = None
        tables = world.database.tables
        return WorldStats(
            tables=len(tables),
            rows=int(world.database.total_rows or sum(t.row_count for t in tables)),
            periods=list(world.periods),
            invariants_passed=sum(1 for c in checks if c.passed),
            invariants_total=len(checks),
            generation_ms=generation_ms,
        )


# --------------------------------------------------------------------------- app services


class AppServices:
    """Process-wide service container.

    Args:
        settings: runtime settings (default :func:`get_settings`).
        persistence: app DB handle (default: ``settings.app_db_path``; schema is initialised).
        pool: benchmark pool (default: loaded lazily from ``settings.benchmarks_dir``).
        dag_path: skill DAG file (default: the packaged ``skill_dag.json``).
        llm_transport: optional ``httpx`` transport for every LLM client (tests: ``FakeTransport``).
        llm_sleep: optional retry sleep for LLM clients (tests pass a no-op).
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        persistence: Persistence | None = None,
        pool: BenchmarkPool | None = None,
        dag_path: str | Path | None = None,
        llm_transport: httpx.BaseTransport | None = None,
        llm_sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.settings.ensure_dirs()
        self.persistence = persistence or Persistence(self.settings.app_db_path)
        self.persistence.init_schema()
        self._pool = pool
        self._pool_lock = threading.Lock()
        self.graph = SkillGraph.load(dag_path, persistence=self.persistence)
        self.graph_lock = threading.RLock()
        self.llm_transport = llm_transport
        self.llm_sleep = llm_sleep
        self._case_locks: dict[str, threading.RLock] = {}
        self._case_locks_guard = threading.Lock()
        self.world_service = WorldService(self)
        logger.info("services ready: data_dir=%s", self.settings.data_dir)

    def close(self) -> None:
        """Release the app DB connection."""
        self.persistence.close()

    # ------------------------------------------------------------------ shared resources

    @property
    def pool(self) -> BenchmarkPool:
        """The benchmark pool (loaded on first use, then cached)."""
        with self._pool_lock:
            if self._pool is None:
                from axiom.engine.market_research import BenchmarkPool

                self._pool = BenchmarkPool.load(self.settings.benchmarks_dir)
            return self._pool

    def case_lock(self, case_id: str) -> threading.RLock:
        """The (re-entrant) lock serializing ``world.json`` writes (and grading) for one case."""
        with self._case_locks_guard:
            lock = self._case_locks.get(case_id)
            if lock is None:
                lock = self._case_locks[case_id] = threading.RLock()
            return lock

    # ------------------------------------------------------------------ LLM

    def llm_settings(self) -> LLMSettings:
        """Stored LLM settings, or the defaults (unconfigured) when none are stored."""
        return self.persistence.get_llm_settings() or LLMSettings()

    def llm_configured(self) -> bool:
        """True when stored settings can make a request."""
        stored = self.persistence.get_llm_settings()
        return bool(stored is not None and stored.configured)

    def llm_client(self, settings: LLMSettings | None = None, *, require_configured: bool = True) -> LLMClient:
        """A fresh client built from the *current* stored settings.

        Raises:
            LLMConfigError: no usable settings are stored (the API maps it to 428).
        """
        current = settings or self.llm_settings()
        if require_configured and not current.configured:
            raise LLMConfigError(LLM_NOT_CONFIGURED)
        kwargs: dict[str, Any] = {}
        if self.llm_sleep is not None:
            kwargs["sleep"] = self.llm_sleep
        return LLMClient(current, self.persistence, transport=self.llm_transport, **kwargs)

    @contextmanager
    def pedagogy(self) -> Iterator[Pedagogy]:
        """A :class:`Pedagogy` around a fresh client; the client is closed afterwards."""
        client = self.llm_client()
        try:
            yield Pedagogy(client, self.graph, self.persistence)
        finally:
            client.close()

    # ------------------------------------------------------------------ sandboxes

    def sql_sandbox(self, case_id: str) -> SQLSandbox:
        """Read-only SQL sandbox for a case."""
        return SQLSandbox(self.world_service.db_path(case_id), timeout_s=self.settings.sandbox_timeout_s)

    def python_sandbox(self, case_id: str) -> PythonSandbox:
        """Python sandbox for a case."""
        db = self.world_service.db_path(case_id)
        return PythonSandbox(db.parent, timeout_s=self.settings.sandbox_timeout_s,
                             memory_mb=self.settings.sandbox_memory_mb, db_path=db,
                             protected_paths=[self.settings.data_dir])
