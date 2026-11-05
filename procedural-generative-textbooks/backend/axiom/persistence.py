"""SQLite persistence for the Axiom app DB (ARCHITECTURE.md section 3).

One ``sqlite3`` connection per ``Persistence`` instance, shared across threads
(``check_same_thread=False``) and serialized with an ``RLock``. All JSON columns are
written with ``sort_keys=True`` so the stored text is stable.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import TracebackType
from typing import Any

from axiom.models import (
    AdversaryTurnRecord,
    CaseRecord,
    CaseSummary,
    LLMSettings,
    LLMUsageRecord,
    LLMUsageSummary,
    MasteryRecord,
    ModelUsage,
    SimQuarter,
    SkillEdge,
    SkillNode,
    Submission,
    SubmissionRecord,
)

__all__ = ["Persistence", "SCHEMA_PATH", "utc_now_iso"]

logger = logging.getLogger("axiom.persistence")

SCHEMA_PATH: Path = Path(__file__).resolve().parent / "schema.sql"


def utc_now_iso() -> str:
    """Current UTC time as an ISO-8601 string (seconds precision)."""
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()


def _dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


class Persistence:
    """Thread-safe typed access to the SQLite app DB."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        """Create a persistence handle. ``db_path=None`` uses ``Settings.app_db_path``;
        ``":memory:"`` gives an in-memory DB."""
        if db_path is None:
            from axiom.config import get_settings

            db_path = get_settings().app_db_path
        assert db_path is not None
        self.db_path: str = str(db_path)
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None

    # ------------------------------------------------------------------ lifecycle

    def connect(self) -> sqlite3.Connection:
        """Open (once) and return the connection: WAL mode, foreign keys on, ``sqlite3.Row`` rows."""
        with self._lock:
            if self._conn is None:
                if self.db_path != ":memory:":
                    Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
                conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30.0)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute("PRAGMA synchronous=NORMAL")
                self._conn = conn
            return self._conn

    def close(self) -> None:
        """Close the connection if open."""
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def __enter__(self) -> Persistence:
        self.connect()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def init_schema(self) -> None:
        """Create all tables (idempotent)."""
        script = SCHEMA_PATH.read_text(encoding="utf-8")
        with self._lock:
            conn = self.connect()
            conn.executescript(script)
            conn.commit()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """Locked transaction: commit on success, roll back on error."""
        with self._lock:
            conn = self.connect()
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def _query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self.connect().execute(sql, tuple(params)).fetchall())

    # ------------------------------------------------------------------ skill DAG

    def upsert_skill_nodes(self, nodes: Iterable[SkillNode]) -> int:
        """Insert or replace skill nodes. Returns the count written."""
        rows = [(n.id, n.pillar, n.title, n.description, n.model_dump_json()) for n in nodes]
        with self._tx() as conn:
            conn.executemany(
                "INSERT INTO skill_nodes (id, pillar, title, description, json) VALUES (?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET pillar=excluded.pillar, title=excluded.title, "
                "description=excluded.description, json=excluded.json",
                rows,
            )
        return len(rows)

    def list_skill_nodes(self) -> list[SkillNode]:
        """All skill nodes ordered by id."""
        return [SkillNode.model_validate_json(r["json"]) for r in self._query("SELECT json FROM skill_nodes ORDER BY id")]

    def replace_skill_edges(self, edges: Iterable[SkillEdge]) -> int:
        """Replace the whole edge set. Returns the count written."""
        rows = [(e.src, e.dst) for e in edges]
        with self._tx() as conn:
            conn.execute("DELETE FROM skill_edges")
            conn.executemany("INSERT OR IGNORE INTO skill_edges (src, dst) VALUES (?,?)", rows)
        return len(rows)

    def list_skill_edges(self) -> list[SkillEdge]:
        """All prerequisite edges ordered by (src, dst)."""
        return [SkillEdge(src=r["src"], dst=r["dst"]) for r in self._query("SELECT src, dst FROM skill_edges ORDER BY src, dst")]

    # ------------------------------------------------------------------ mastery

    def get_mastery_all(self) -> dict[str, MasteryRecord]:
        """Mastery rows keyed by node id."""
        rows = self._query("SELECT node_id, p_known, attempts, correct, last_seen FROM mastery ORDER BY node_id")
        return {r["node_id"]: MasteryRecord(**dict(r)) for r in rows}

    def get_mastery(self, node_id: str) -> MasteryRecord | None:
        """Mastery for one node, or ``None``."""
        rows = self._query("SELECT node_id, p_known, attempts, correct, last_seen FROM mastery WHERE node_id=?", (node_id,))
        return MasteryRecord(**dict(rows[0])) if rows else None

    def upsert_mastery(self, record: MasteryRecord) -> None:
        """Insert or replace one mastery row (``last_seen`` defaults to now)."""
        last_seen = record.last_seen or utc_now_iso()
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO mastery (node_id, p_known, attempts, correct, last_seen) VALUES (?,?,?,?,?) "
                "ON CONFLICT(node_id) DO UPDATE SET p_known=excluded.p_known, attempts=excluded.attempts, "
                "correct=excluded.correct, last_seen=excluded.last_seen",
                (record.node_id, record.p_known, record.attempts, record.correct, last_seen),
            )

    def reset_mastery(self) -> int:
        """Delete all mastery rows. Returns the number deleted."""
        with self._tx() as conn:
            cur = conn.execute("DELETE FROM mastery")
            return cur.rowcount

    # ------------------------------------------------------------------ cases

    def upsert_case(self, record: CaseRecord) -> None:
        """Insert or replace a case row."""
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO cases (case_id, seed, archetype, difficulty, company_name, industry, pillars_json, "
                "skill_nodes_json, world_path, db_path, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(case_id) DO UPDATE SET seed=excluded.seed, archetype=excluded.archetype, "
                "difficulty=excluded.difficulty, company_name=excluded.company_name, industry=excluded.industry, "
                "pillars_json=excluded.pillars_json, skill_nodes_json=excluded.skill_nodes_json, "
                "world_path=excluded.world_path, db_path=excluded.db_path, status=excluded.status",
                (
                    record.case_id,
                    record.seed,
                    record.archetype,
                    record.difficulty,
                    record.company_name,
                    record.industry,
                    _dumps(list(record.pillars)),
                    _dumps(list(record.skill_nodes)),
                    record.world_path,
                    record.db_path,
                    record.status,
                    record.created_at or utc_now_iso(),
                ),
            )

    @staticmethod
    def _case_from_row(row: sqlite3.Row) -> CaseRecord:
        data = dict(row)
        data["pillars"] = json.loads(data.pop("pillars_json"))
        data["skill_nodes"] = json.loads(data.pop("skill_nodes_json"))
        return CaseRecord.model_validate(data)

    def get_case(self, case_id: str) -> CaseRecord | None:
        """One case row, or ``None``."""
        rows = self._query("SELECT * FROM cases WHERE case_id=?", (case_id,))
        return self._case_from_row(rows[0]) if rows else None

    def list_cases(self, limit: int | None = None) -> list[CaseSummary]:
        """Case summaries, newest first."""
        sql = "SELECT * FROM cases ORDER BY created_at DESC, case_id DESC"
        params: tuple[Any, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (int(limit),)
        return [self._case_from_row(r).summary() for r in self._query(sql, params)]

    def set_case_status(self, case_id: str, status: str) -> bool:
        """Update a case's status. Returns True if a row changed."""
        with self._tx() as conn:
            return conn.execute("UPDATE cases SET status=? WHERE case_id=?", (status, case_id)).rowcount > 0

    def delete_case(self, case_id: str) -> bool:
        """Delete a case and (by cascade) its artifacts, submissions, turns and quarters."""
        with self._tx() as conn:
            return conn.execute("DELETE FROM cases WHERE case_id=?", (case_id,)).rowcount > 0

    # ------------------------------------------------------------------ artifacts

    def save_artifact(self, case_id: str, kind: str, key: str, payload: dict[str, Any]) -> None:
        """Insert or replace a cached artifact (dossier, textbook chapter, ...)."""
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO case_artifacts (case_id, kind, key, json, created_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(case_id, kind, key) DO UPDATE SET json=excluded.json, created_at=excluded.created_at",
                (case_id, kind, key, _dumps(payload), utc_now_iso()),
            )

    def get_artifact(self, case_id: str, kind: str, key: str = "") -> dict[str, Any] | None:
        """A cached artifact payload, or ``None``."""
        rows = self._query("SELECT json FROM case_artifacts WHERE case_id=? AND kind=? AND key=?", (case_id, kind, key))
        return json.loads(rows[0]["json"]) if rows else None

    def list_artifacts(self, case_id: str, kind: str | None = None) -> list[tuple[str, str]]:
        """``(kind, key)`` pairs stored for a case."""
        if kind is None:
            rows = self._query("SELECT kind, key FROM case_artifacts WHERE case_id=? ORDER BY kind, key", (case_id,))
        else:
            rows = self._query("SELECT kind, key FROM case_artifacts WHERE case_id=? AND kind=? ORDER BY key", (case_id, kind))
        return [(r["kind"], r["key"]) for r in rows]

    def delete_artifact(self, case_id: str, kind: str, key: str = "") -> bool:
        """Remove a cached artifact. Returns True if it existed."""
        with self._tx() as conn:
            return conn.execute(
                "DELETE FROM case_artifacts WHERE case_id=? AND kind=? AND key=?", (case_id, kind, key)
            ).rowcount > 0

    # ------------------------------------------------------------------ submissions

    def save_submission(self, submission_id: str, submission: Submission, grade: dict[str, Any] | None = None) -> None:
        """Insert or update a submission (and its grade, once available)."""
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO submissions (submission_id, case_id, submitted_json, grade_json, created_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(submission_id) DO UPDATE SET submitted_json=excluded.submitted_json, "
                "grade_json=COALESCE(excluded.grade_json, submissions.grade_json)",
                (
                    submission_id,
                    submission.case_id,
                    submission.model_dump_json(),
                    _dumps(grade) if grade is not None else None,
                    utc_now_iso(),
                ),
            )

    def set_submission_grade(self, submission_id: str, grade: dict[str, Any]) -> bool:
        """Attach a grade to an existing submission."""
        with self._tx() as conn:
            return conn.execute(
                "UPDATE submissions SET grade_json=? WHERE submission_id=?", (_dumps(grade), submission_id)
            ).rowcount > 0

    @staticmethod
    def _submission_from_row(row: sqlite3.Row) -> SubmissionRecord:
        return SubmissionRecord(
            submission_id=row["submission_id"],
            case_id=row["case_id"],
            submitted=Submission.model_validate_json(row["submitted_json"]),
            grade=json.loads(row["grade_json"]) if row["grade_json"] else None,
            created_at=row["created_at"],
        )

    def get_submission(self, submission_id: str) -> SubmissionRecord | None:
        """One submission, or ``None``."""
        rows = self._query("SELECT * FROM submissions WHERE submission_id=?", (submission_id,))
        return self._submission_from_row(rows[0]) if rows else None

    def list_submissions(self, case_id: str) -> list[SubmissionRecord]:
        """Submissions for a case, oldest first."""
        rows = self._query("SELECT * FROM submissions WHERE case_id=? ORDER BY created_at, submission_id", (case_id,))
        return [self._submission_from_row(r) for r in rows]

    # ------------------------------------------------------------------ adversary turns

    def append_adversary_turn(self, submission_id: str, role: str, payload: dict[str, Any], turn_index: int | None = None) -> AdversaryTurnRecord:
        """Append a turn; ``turn_index`` defaults to the next index for that submission."""
        with self._tx() as conn:
            if turn_index is None:
                row = conn.execute(
                    "SELECT COALESCE(MAX(turn_index), -1) + 1 AS nxt FROM adversary_turns WHERE submission_id=?",
                    (submission_id,),
                ).fetchone()
                turn_index = int(row["nxt"])
            created = utc_now_iso()
            cur = conn.execute(
                "INSERT INTO adversary_turns (submission_id, turn_index, role, json, created_at) VALUES (?,?,?,?,?)",
                (submission_id, turn_index, role, _dumps(payload), created),
            )
            new_id = cur.lastrowid
        return AdversaryTurnRecord(
            id=new_id, submission_id=submission_id, turn_index=turn_index, role=role,  # type: ignore[arg-type]
            payload=payload, created_at=created,
        )

    def list_turns(self, submission_id: str) -> list[AdversaryTurnRecord]:
        """All turns for a submission ordered by (turn_index, id)."""
        rows = self._query(
            "SELECT * FROM adversary_turns WHERE submission_id=? ORDER BY turn_index, id", (submission_id,)
        )
        return [
            AdversaryTurnRecord(
                id=r["id"], submission_id=r["submission_id"], turn_index=r["turn_index"], role=r["role"],
                payload=json.loads(r["json"]), created_at=r["created_at"],
            )
            for r in rows
        ]

    # ------------------------------------------------------------------ sim quarters

    def save_sim_quarter(self, case_id: str, quarter: SimQuarter, submission_id: str | None = None) -> int:
        """Append a simulated quarter. Returns the row id."""
        with self._tx() as conn:
            cur = conn.execute(
                "INSERT INTO sim_quarters (case_id, submission_id, period, decisions_json, kpis_json, narrative, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (case_id, submission_id, quarter.period, _dumps(quarter.decisions), _dumps(quarter.kpis), quarter.narrative, utc_now_iso()),
            )
            return int(cur.lastrowid or 0)

    def list_sim_quarters(self, case_id: str, submission_id: str | None = None) -> list[SimQuarter]:
        """Quarters for a case (optionally one submission's run), in insertion order."""
        if submission_id is None:
            rows = self._query("SELECT * FROM sim_quarters WHERE case_id=? ORDER BY id", (case_id,))
        else:
            rows = self._query(
                "SELECT * FROM sim_quarters WHERE case_id=? AND submission_id=? ORDER BY id", (case_id, submission_id)
            )
        return [
            SimQuarter(period=r["period"], decisions=json.loads(r["decisions_json"]), kpis=json.loads(r["kpis_json"]), narrative=r["narrative"])
            for r in rows
        ]

    # ------------------------------------------------------------------ LLM settings / usage

    def get_llm_settings(self) -> LLMSettings | None:
        """Stored LLM settings (unmasked), or ``None`` if never configured."""
        rows = self._query("SELECT json FROM llm_settings WHERE id=1")
        return LLMSettings.model_validate_json(rows[0]["json"]) if rows else None

    def put_llm_settings(self, settings: LLMSettings) -> None:
        """Store LLM settings (single row). The API key is never logged."""
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO llm_settings (id, json) VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET json=excluded.json",
                (settings.model_dump_json(),),
            )
        logger.info("llm settings updated: provider=%s fast=%s deep=%s", settings.provider, settings.fast_model, settings.deep_model)

    def record_usage(self, record: LLMUsageRecord) -> int:
        """Append an LLM usage row. Returns the row id."""
        with self._tx() as conn:
            cur = conn.execute(
                "INSERT INTO llm_usage (ts, model, purpose, prompt_tokens, completion_tokens, latency_ms, cost_usd_est) "
                "VALUES (?,?,?,?,?,?,?)",
                (record.ts or utc_now_iso(), record.model, record.purpose, record.prompt_tokens,
                 record.completion_tokens, record.latency_ms, record.cost_usd_est),
            )
            return int(cur.lastrowid or 0)

    def list_usage(self, limit: int = 100) -> list[LLMUsageRecord]:
        """Most recent usage rows, newest first."""
        rows = self._query("SELECT * FROM llm_usage ORDER BY id DESC LIMIT ?", (int(limit),))
        return [LLMUsageRecord(**dict(r)) for r in rows]

    def usage_summary(self) -> LLMUsageSummary:
        """Aggregate usage by model."""
        rows = self._query(
            "SELECT model, COUNT(*) AS calls, SUM(prompt_tokens) AS pt, SUM(completion_tokens) AS ct, "
            "AVG(latency_ms) AS lat, SUM(cost_usd_est) AS cost FROM llm_usage GROUP BY model ORDER BY model"
        )
        by_model: dict[str, ModelUsage] = {}
        total_tokens = 0
        total_cost = 0.0
        for r in rows:
            pt, ct = int(r["pt"] or 0), int(r["ct"] or 0)
            usage = ModelUsage(
                calls=int(r["calls"]), prompt_tokens=pt, completion_tokens=ct, total_tokens=pt + ct,
                avg_latency_ms=float(r["lat"] or 0.0), cost_usd_est=float(r["cost"] or 0.0),
            )
            by_model[r["model"]] = usage
            total_tokens += usage.total_tokens
            total_cost += usage.cost_usd_est
        return LLMUsageSummary(by_model=by_model, total_tokens=total_tokens, total_cost_usd_est=round(total_cost, 6))
