"""Every registered archetype x difficulty x seed: invariants, ground truth, dossier, simulation, determinism."""

from __future__ import annotations

import json
import time
from pathlib import Path

import duckdb
import pytest

from axiom.engine.archetypes import ARCHETYPE_MODULES, ARCHETYPE_REGISTRY, catalog
from axiom.engine.archetypes.base import SIM_MODEL_KEYS, sql_signature
from axiom.engine.market_research import get_default_pool
from axiom.engine.procedural_world import generate_world, load_world, make_case_id, resolve_db_path, world_json_bytes
from axiom.models import CaseWorld

ARCHETYPES = sorted(ARCHETYPE_REGISTRY)
DIFFICULTIES = ("foundation", "practitioner", "principal")
SEEDS = (1, 7, 2024)


@pytest.fixture(scope="module")
def worlds(tmp_path_factory: pytest.TempPathFactory) -> dict[tuple[str, str, int], tuple[CaseWorld, Path]]:
    """Generate each (archetype, difficulty, seed) once for the whole module."""
    pool = get_default_pool()
    root = tmp_path_factory.mktemp("worlds")
    out: dict[tuple[str, str, int], tuple[CaseWorld, Path]] = {}
    for a in ARCHETYPES:
        for d in DIFFICULTIES:
            for s in SEEDS:
                folder = root / f"{a}-{d}-{s}"
                out[(a, d, s)] = (generate_world(a, s, difficulty=d, out_dir=folder, pool=pool), folder)
    return out


def test_registry_and_catalog() -> None:
    assert {"working_capital_crunch", "idempotent_webhooks"} <= set(ARCHETYPE_REGISTRY)
    infos = catalog()
    assert [i.key for i in infos] == [k for k in ARCHETYPE_MODULES if k in ARCHETYPE_REGISTRY]
    for info in infos:
        assert info.title and info.pillars and info.skill_nodes and info.difficulty_range == list(DIFFICULTIES) and info.summary


@pytest.mark.parametrize("archetype", ARCHETYPES)
@pytest.mark.parametrize("difficulty", DIFFICULTIES)
@pytest.mark.parametrize("seed", SEEDS)
def test_world(worlds: dict, archetype: str, difficulty: str, seed: int) -> None:
    world, folder = worlds[(archetype, difficulty, seed)]
    assert world.case_id == make_case_id(archetype, seed, difficulty)
    assert world.generated_at
    # invariants
    assert world.finance.invariants and all(c.passed for c in world.finance.invariants)
    for b in world.finance.statements.balance:
        assert b.total_assets_cents == b.total_liabilities_cents + b.total_equity_cents
    assert len(world.periods) == 8 and world.finance.statements.periods == world.periods
    # ground truth
    gt = world.ground_truth
    assert len(gt.expected_metrics) >= 3
    assert all(m.derivation and m.pillar for m in gt.expected_metrics)
    assert gt.root_cause and gt.decision_levers and gt.red_herrings
    assert len(gt.red_herrings) == {"foundation": 1, "practitioner": 2, "principal": 3}[difficulty]
    con = duckdb.connect(str(resolve_db_path(world, folder)), read_only=True)
    try:
        n_queries = 0
        for a in gt.anomalies:
            for q in a.reference_sql:
                n_queries += 1
                rows = con.execute(q.sql).fetchall()
                assert rows, q.name
                assert q.expected and q.expected["row_count"] == len(rows) and q.expected["signature"]
                for construct in q.expected["signature"]:
                    assert construct in " ".join(q.sql.lower().split()).replace(" (", "("), (q.name, construct)
        assert n_queries >= 3
        tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
        assert {"period_calendar", "gl_journal", "gl_lines", "gl_accounts"} <= tables
        assert con.execute("SELECT last_day_of_quarter(DATE '2024-02-10')").fetchone()[0].isoformat() == "2024-03-31"
        gl_lines = con.execute("SELECT COUNT(*) FROM gl_lines").fetchone()[0]
        assert gl_lines == sum(len(e.lines) for e in world.finance.journal)
    finally:
        con.close()
    assert world.database.total_rows <= {"foundation": 13_000, "practitioner": 32_000, "principal": 50_000}[difficulty]
    applied = [r for t in world.database.tables for r in t.dirt]
    assert len(applied) == {"foundation": 2, "practitioner": 4, "principal": 6}[difficulty]
    # dossier seed
    ds = world.dossier_seed
    conceal = " ".join(ds.conceal).lower()
    for term in ARCHETYPE_REGISTRY[archetype].root_cause_terms:
        assert term in conceal
    assert ds.reveal and ds.stakeholders and ds.timeline and ds.artifacts
    if "system_design" in world.pillars:
        assert ds.architecture_mermaid and ds.architecture_mermaid.startswith("flowchart")
    # simulation model
    model = world.simulation.model
    assert set(SIM_MODEL_KEYS) <= set(model)
    levers = {lv.key for lv in gt.decision_levers}
    assert all(e["lever"] in levers and e["kpi"] in model["kpis"] for e in model["lever_effects"])
    assert world.simulation.current_period == world.periods[-1]
    # public view
    pub = world.public_view()
    assert "ground_truth" not in pub and "conceal" not in pub["dossier_seed"]
    assert world.to_public().case_id == world.case_id
    # world.json on disk round-trips
    on_disk = CaseWorld.model_validate_json((folder / "world.json").read_bytes())
    assert on_disk.model_copy(update={"generated_at": world.generated_at}) == world


@pytest.mark.parametrize("archetype", ARCHETYPES)
def test_determinism(tmp_path: Path, worlds: dict, archetype: str) -> None:
    pool = get_default_pool()
    world, folder = worlds[(archetype, "foundation", 7)]
    again = generate_world(archetype, 7, difficulty="foundation", out_dir=tmp_path / "again", pool=pool)
    assert (tmp_path / "again" / "world.json").read_bytes() == (folder / "world.json").read_bytes()
    assert world_json_bytes(again) == world_json_bytes(world)
    other = worlds[(archetype, "foundation", 1)][1]
    assert (other / "world.json").read_bytes() != (folder / "world.json").read_bytes()


@pytest.mark.parametrize("archetype", ARCHETYPES)
def test_practitioner_generation_speed(tmp_path: Path, archetype: str) -> None:
    t0 = time.perf_counter()
    generate_world(archetype, 99, difficulty="practitioner", out_dir=tmp_path, pool=get_default_pool())
    assert time.perf_counter() - t0 < 10.0


def test_working_capital_story_holds(worlds: dict) -> None:
    for s in SEEDS:
        world, _ = worlds[("working_capital_crunch", "practitioner", s)]
        r = world.finance.statements.ratios
        p5, p8 = world.periods[4], world.periods[7]
        inc = {i.period: i for i in world.finance.statements.income}
        assert all(inc[p].net_income_cents > 0 for p in world.periods[4:])  # profitable...
        assert r[p8]["dso"] > r[p5]["dso"] and r[p8]["ccc"] > r[p5]["ccc"]  # ...but the CCC lengthened
        m = {x.key: x.value for x in world.ground_truth.expected_metrics}
        assert m["cfo_minus_ni_p6_p8_cents"] < 0 and m["delta_wc_cash_drag_p6_p8_cents"] > 0
        assert m["revolver_utilization_p8"] > 0.9


def test_webhook_story_holds(worlds: dict) -> None:
    for d in DIFFICULTIES:
        world, folder = worlds[("idempotent_webhooks", d, 7)]
        m = {x.key: x.value for x in world.ground_truth.expected_metrics}
        assert m["duplicate_charge_count"] > 0 and m["dedupe_present_code"] == (1.0 if d == "principal" else 0.0)
        breaks = [c for c in world.finance.invariants if c.name.startswith("subledger:payments_ledger_vs_gl_volume")]
        assert breaks and all(c.passed for c in breaks) and any("designed break" in c.detail for c in breaks)
        q = world.ground_truth.anomalies[0].reference_sql[0]
        assert q.expected["checks"]["duplicate_rows"] == m["duplicate_charge_count"]


def test_sql_signature_detects_constructs() -> None:
    sig = sql_signature("WITH a AS (SELECT x, ROW_NUMBER() OVER (PARTITION BY y) FROM t LEFT JOIN u USING (k)) SELECT * FROM a GROUP BY 1 HAVING COUNT(*) > 1")
    assert sig[:3] == ["over(", "partition by", "row_number("] and len(sig) == 5


def test_simulation_model_runs_in_quarter_simulator(worlds: dict) -> None:
    """Integration with Module 3's simulator when it is importable (it is built concurrently)."""
    try:
        from axiom.engine.pedagogy import simulate_transition
    except Exception as exc:  # pragma: no cover - depends on Module 3 state
        pytest.skip(f"pedagogy not importable: {exc}")
    for a in ARCHETYPES:
        world, _ = worlds[(a, "practitioner", 1)]
        levers = world.ground_truth.decision_levers
        decisions = {lv.key: lv.max for lv in levers}
        q, info = simulate_transition(world.simulation, levers, decisions)
        assert info["resolved"] and set(world.simulation.model["kpis"]) <= set(q.kpis)
        json.dumps(q.model_dump(mode="json"))


def test_load_world_from_cases_dir(tmp_path: Path) -> None:
    world = generate_world("idempotent_webhooks", 3, difficulty="foundation", out_dir=tmp_path / make_case_id("idempotent_webhooks", 3, "foundation"))
    loaded = load_world(world.case_id, cases_dir=tmp_path)
    assert loaded == world and loaded.generated_at == world.generated_at
    with pytest.raises(FileNotFoundError):
        load_world("nope", cases_dir=tmp_path)
