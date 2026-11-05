"""Deterministic world generation entry point and CLI (ARCHITECTURE.md sections 1, 3, 6).

``generate_world(archetype, seed)`` builds a :class:`~axiom.models.CaseWorld`, writes
``<cases_dir>/<case_id>/company.duckdb`` and ``world.json`` and returns the world.

Determinism: the root stream is ``SeededRNG.for_world(archetype, seed, version)``; nothing reads the
wall clock during generation. ``generated_at`` is stamped *after* generation on the returned object
and stored in the sidecar ``meta.json``; ``world.json`` itself is written with ``generated_at=""``
so that the same ``(archetype, seed, difficulty, industry)`` always produces byte-identical
``world.json`` (section 6.5). :func:`load_world` restores ``generated_at`` from ``meta.json``.

CLI::

    python -m axiom.engine.procedural_world generate --archetype working_capital_crunch --seed 42 --difficulty practitioner
    python -m axiom.engine.procedural_world list
    python -m axiom.engine.procedural_world verify --archetype working_capital_crunch --seed 42
    python -m axiom.engine.procedural_world inspect <case_id>
    python -m axiom.engine.procedural_world bench saas.dso --industry saas
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import logging
import sys
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path

from axiom import __version__
from axiom.config import Settings, get_settings
from axiom.engine import benchmark_keys
from axiom.engine.archetypes import catalog, get_archetype
from axiom.engine.archetypes.base import DIFFICULTIES, BuildContext
from axiom.engine.market_research import BenchmarkPool, get_default_pool
from axiom.engine.rng import SeededRNG
from axiom.models import CaseWorld, Difficulty

__all__ = ["make_case_id", "generate_world", "world_json_bytes", "resolve_db_path", "load_world", "main"]

logger = logging.getLogger("axiom.procedural_world")

WORLD_FILE = "world.json"
META_FILE = "meta.json"
DB_FILE = "company.duckdb"


def make_case_id(archetype: str, seed: int, difficulty: str, industry: str | None = None, version: str = __version__) -> str:
    """``f"{archetype}-{seed}-{difficulty}"[:40]`` + ``-`` + 8 hex chars of BLAKE2b over all inputs."""
    digest = hashlib.blake2b(f"{archetype}|{seed}|{difficulty}|{industry or ''}|{version}".encode(), digest_size=4).hexdigest()
    return f"{f'{archetype}-{seed}-{difficulty}'[:40]}-{digest}"


def world_json_bytes(world: CaseWorld) -> bytes:
    """Canonical ``world.json`` bytes (``generated_at`` blanked so the file is reproducible)."""
    return world.model_copy(update={"generated_at": ""}).model_dump_json(indent=1).encode("utf-8")


def generate_world(
    archetype: str,
    seed: int,
    *,
    difficulty: Difficulty = "practitioner",
    industry: str | None = None,
    out_dir: Path | None = None,
    pool: BenchmarkPool | None = None,
    settings: Settings | None = None,
) -> CaseWorld:
    """Generate, validate and persist one world; returns it with ``generated_at`` stamped.

    Raises ``KeyError`` for an unknown archetype, ``ValueError`` for an unsupported
    difficulty/industry, and ``WorldInvariantError`` if the generator produced an invalid world
    (nothing is written in that case).
    """
    if difficulty not in DIFFICULTIES:
        raise ValueError(f"difficulty must be one of {DIFFICULTIES}")
    settings = settings or get_settings()
    pool = pool or get_default_pool()
    arch = get_archetype(archetype)
    case_id = make_case_id(archetype, seed, difficulty, industry)
    assert settings.cases_dir is not None
    out = Path(out_dir) if out_dir is not None else Path(settings.cases_dir) / case_id
    out.mkdir(parents=True, exist_ok=True)
    ctx = BuildContext(rng=SeededRNG.for_world(archetype, int(seed), __version__), pool=pool, settings=settings,
                       difficulty=difficulty, case_id=case_id, out_dir=out, seed=int(seed), industry=industry)
    t0 = time.perf_counter()
    world = arch.build(ctx)
    elapsed = time.perf_counter() - t0
    (out / WORLD_FILE).write_bytes(world_json_bytes(world))
    stamped = dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()
    (out / META_FILE).write_text(json.dumps({"case_id": case_id, "generated_at": stamped, "build_seconds": round(elapsed, 3),
                                             "archetype": archetype, "seed": int(seed), "difficulty": difficulty}, indent=1), encoding="utf-8")
    logger.info("generated %s in %.2fs (%d rows)", case_id, elapsed, world.database.total_rows)
    return world.model_copy(update={"generated_at": stamped})


def resolve_db_path(world: CaseWorld, case_dir: Path | None = None, *, settings: Settings | None = None) -> Path:
    """Actual ``company.duckdb`` path: ``case_dir/company.duckdb`` when the case folder is known,
    else ``settings.data_dir / world.database.path``."""
    if case_dir is not None:
        return Path(case_dir) / DB_FILE
    settings = settings or get_settings()
    return Path(settings.data_dir) / world.database.path


def load_world(case_id: str, *, settings: Settings | None = None, cases_dir: Path | None = None) -> CaseWorld:
    """Load ``<cases_dir>/<case_id>/world.json`` (``generated_at`` restored from ``meta.json``)."""
    settings = settings or get_settings()
    root = Path(cases_dir) if cases_dir is not None else Path(settings.cases_dir)  # type: ignore[arg-type]
    folder = root / case_id
    path = folder / WORLD_FILE
    if not path.exists():
        raise FileNotFoundError(f"no world.json for case {case_id!r} under {root}")
    world = CaseWorld.model_validate_json(path.read_bytes())
    meta = folder / META_FILE
    if meta.exists():
        stamp = json.loads(meta.read_text(encoding="utf-8")).get("generated_at", "")
        world = world.model_copy(update={"generated_at": stamp})
    return world


# --------------------------------------------------------------------------- CLI helpers


def _money(c: int) -> str:
    return f"{c / 100_000:,.1f}"


def _table(title: str, periods: Sequence[str], rows: Sequence[tuple[str, Sequence[str]]]) -> str:
    width = max([len(r[0]) for r in rows] + [len(title)]) + 2
    out = [title.ljust(width) + "".join(p.rjust(12) for p in periods), "-" * (width + 12 * len(periods))]
    out += [label.ljust(width) + "".join(v.rjust(12) for v in vals) for label, vals in rows]
    return "\n".join(out)


def render_statements(world: CaseWorld) -> str:
    """IS / BS / CFS as aligned text tables ($ thousands) plus key ratios."""
    st = world.finance.statements
    periods = st.periods

    def rows_for(stmts: Sequence) -> list[tuple[str, list[str]]]:
        caps = [ln.caption for ln in stmts[0].lines]
        return [(("  " if stmts[0].lines[i].level == 0 else "") + cap, [_money(s.lines[i].cents) for s in stmts]) for i, cap in enumerate(caps)]

    ratio_keys = ["gross_margin", "operating_margin", "dso", "dio", "dpo", "ccc", "current_ratio", "debt_to_ebitda", "m_score"]
    ratios = [(k, [f"{st.ratios[p].get(k, float('nan')):.2f}" for p in periods]) for k in ratio_keys]
    return "\n\n".join([
        _table("INCOME STATEMENT ($k)", periods, rows_for(st.income)),
        _table("BALANCE SHEET ($k)", periods, rows_for(st.balance)),
        _table("CASH FLOW (indirect, $k)", periods, rows_for(st.cash_flow)),
        _table("RATIOS", periods, ratios),
    ])


def render_answer_key(world: CaseWorld) -> str:
    """Root cause, anomalies, expected metrics and red herrings."""
    gt = world.ground_truth
    lines = ["ANOMALY KEY", "=" * 11, gt.root_cause, ""]
    for a in gt.anomalies:
        lines.append(f"[{a.id}] {a.title} ({a.kind}, {a.pillar}, {a.severity}) periods={','.join(a.periods)}")
        lines.append(f"     {a.mechanism}")
        for q in a.reference_sql:
            lines.append(f"     ref SQL {q.name}: {q.expected.get('row_count')} rows; signature={q.expected.get('signature')}; checks={q.expected.get('checks', {})}")
    lines += ["", "EXPECTED METRICS"]
    for m in gt.expected_metrics:
        tol = f"abs {m.tolerance_abs}" if m.tolerance_abs is not None else f"rel {m.tolerance_rel}"
        lines.append(f"  {m.key:<40} {m.value:>18,.4f} {m.unit:<7} ({tol}) [{m.pillar}]")
    lines += ["", "RED HERRINGS"] + [f"  - {h}" for h in gt.red_herrings]
    return "\n".join(lines)


def invariant_report(world: CaseWorld) -> str:
    """Summary of the invariant checks."""
    checks = world.finance.invariants
    failed = [c for c in checks if not c.passed]
    groups: dict[str, int] = {}
    for c in checks:
        groups[c.name.split("[")[0]] = groups.get(c.name.split("[")[0], 0) + 1
    lines = [f"invariants: {len(checks) - len(failed)}/{len(checks)} passed"]
    lines += [f"  {name}: {n}" for name, n in sorted(groups.items())]
    lines += [f"  FAILED {c.name}: {c.detail}" for c in failed]
    designed = [c for c in checks if "designed break" in c.detail]
    lines += [f"  designed break verified {c.name}: {c.detail}" for c in designed[:8]]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(prog="python -m axiom.engine.procedural_world", description="Axiom deterministic world generator")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    g = sub.add_parser("generate", help="generate a world")
    g.add_argument("--archetype", required=True)
    g.add_argument("--seed", type=int, required=True)
    g.add_argument("--difficulty", default="practitioner", choices=DIFFICULTIES)
    g.add_argument("--industry", default=None)
    g.add_argument("--out", type=Path, default=None)
    sub.add_parser("list", help="list registered archetypes")
    v = sub.add_parser("verify", help="generate twice and diff world.json byte-for-byte")
    v.add_argument("--archetype", required=True)
    v.add_argument("--seed", type=int, required=True)
    v.add_argument("--difficulty", default="practitioner", choices=DIFFICULTIES)
    v.add_argument("--industry", default=None)
    i = sub.add_parser("inspect", help="print statements and the answer key of a generated case")
    i.add_argument("case_id")
    b = sub.add_parser("bench", help="explain how a spec benchmark key resolves")
    b.add_argument("key")
    b.add_argument("--industry", default="any")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    if args.command == "list":
        for info in catalog():
            print(f"{info.key:<26} {info.title:<28} pillars={','.join(info.pillars)} difficulty={','.join(info.difficulty_range)}")
            print(f"{'':<26} {info.summary}")
        return 0
    if args.command == "generate":
        t0 = time.perf_counter()
        world = generate_world(args.archetype, args.seed, difficulty=args.difficulty, industry=args.industry, out_dir=args.out)
        print(json.dumps({"case_id": world.case_id, "company": world.company.name, "industry": world.industry.code,
                          "periods": world.periods, "rows": world.database.total_rows,
                          "db": str(resolve_db_path(world, args.out) if args.out else resolve_db_path(world)),
                          "seconds": round(time.perf_counter() - t0, 2)}, indent=1))
        return 0
    if args.command == "verify":
        with tempfile.TemporaryDirectory() as d1, tempfile.TemporaryDirectory() as d2:
            t0 = time.perf_counter()
            w1 = generate_world(args.archetype, args.seed, difficulty=args.difficulty, industry=args.industry, out_dir=Path(d1))
            t1 = time.perf_counter()
            generate_world(args.archetype, args.seed, difficulty=args.difficulty, industry=args.industry, out_dir=Path(d2))
            b1 = (Path(d1) / WORLD_FILE).read_bytes()
            b2 = (Path(d2) / WORLD_FILE).read_bytes()
        same = b1 == b2
        print(invariant_report(w1))
        print(f"world.json: {len(b1):,} bytes; identical={same}; first build {t1 - t0:.2f}s; rows={w1.database.total_rows}")
        if not same:
            idx = next(k for k in range(min(len(b1), len(b2))) if b1[k] != b2[k]) if b1[:min(len(b1), len(b2))] != b2[:min(len(b1), len(b2))] else min(len(b1), len(b2))
            print(f"first difference at byte {idx}: {b1[max(0, idx - 80):idx + 80]!r}\nvs {b2[max(0, idx - 80):idx + 80]!r}")
        return 0 if same and all(c.passed for c in w1.finance.invariants) else 1
    if args.command == "inspect":
        world = load_world(args.case_id)
        print(f"{world.case_id}: {world.company.name} ({world.industry.code}, {world.difficulty}) - {world.company.description}\n")
        print(render_statements(world))
        print()
        print(render_answer_key(world))
        return 0
    if args.command == "bench":
        for line in benchmark_keys.explain(args.key, args.industry):
            print(line)
        return 0
    parser.error(f"unknown command {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())

