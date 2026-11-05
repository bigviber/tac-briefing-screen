"""Targeted checks for ``monte_carlo_cash`` beyond the generic archetype suite."""

from __future__ import annotations

import itertools
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import duckdb
import pytest

from axiom.engine.market_research import get_default_pool
from axiom.engine.procedural_world import generate_world, resolve_db_path
from axiom.models import CaseWorld

KEY = "monte_carlo_cash"
DIFFICULTIES = ("foundation", "practitioner", "principal")
SEEDS = (1, 7, 2024)

# Mirrors the Python sandbox namespace (pd, np, duckdb, scipy, con, tables) in an isolated interpreter.
_RUNNER = textwrap.dedent('''
    import json, sys
    import numpy as np, pandas as pd, duckdb
    db, code_path, out_path = sys.argv[1:4]
    con = duckdb.connect(db, read_only=True)
    class Tables(dict):
        def __getitem__(self, k):
            if not dict.__contains__(self, k):
                dict.__setitem__(self, k, con.execute(f'SELECT * FROM "{k}"').df())
            return dict.__getitem__(self, k)
    ns = {"pd": pd, "np": np, "duckdb": duckdb, "con": con, "tables": Tables(), "__name__": "__main__"}
    exec(compile(open(code_path).read(), "<reference>", "exec"), ns)
    json.dump(ns["result"], open(out_path, "w"), default=float)
''')


def run_isolated(code: str, db_path: Path, tmp: Path) -> dict:
    (tmp / "runner.py").write_text(_RUNNER)
    (tmp / "code.py").write_text(code)
    proc = subprocess.run([sys.executable, "-I", str(tmp / "runner.py"), str(db_path), str(tmp / "code.py"), str(tmp / "out.json")],
                          capture_output=True, text=True, check=False, timeout=180, cwd=tmp)
    assert proc.returncode == 0, proc.stderr[-3000:]
    return json.loads((tmp / "out.json").read_text())


@pytest.fixture(scope="module")
def worlds(tmp_path_factory: pytest.TempPathFactory) -> dict[tuple[str, int], tuple[CaseWorld, Path]]:
    pool = get_default_pool()
    root = tmp_path_factory.mktemp("mc")
    return {(d, s): (generate_world(KEY, s, difficulty=d, out_dir=root / f"{d}-{s}", pool=pool), root / f"{d}-{s}")
            for d in DIFFICULTIES for s in SEEDS}


def _metrics(world: CaseWorld) -> dict[str, float]:
    return {m.key: m.value for m in world.ground_truth.expected_metrics}


def _fetch(world: CaseWorld, folder: Path, sql: str) -> list[tuple]:
    con = duckdb.connect(str(resolve_db_path(world, folder)), read_only=True)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


@pytest.mark.parametrize("difficulty", DIFFICULTIES)
def test_reference_python_reproduces_expected_metrics_in_isolated_subprocess(worlds: dict, difficulty: str, tmp_path: Path) -> None:
    """The learner-facing simulation, run with ``python -I`` on the dirty DuckDB, gives the graded numbers."""
    world, folder = worlds[(difficulty, 7)]
    m = _metrics(world)
    code = world.ground_truth.reference_solution.python[0]
    assert "tables" in code or "con" in code
    res = run_isolated(code, resolve_db_path(world, folder), tmp_path)
    for key in ("p_shortfall_13w", "p_covenant_breach_13w"):
        assert res[key] == pytest.approx(m[key], abs=1e-3), key
    for key in ("expected_min_cash_cents", "p5_min_cash_cents", "cash_needed_for_p95_safety_cents"):
        assert res[key] == pytest.approx(m[key], rel=1e-5, abs=100), key
    for key in ("collection_delay_gamma_k", "collection_delay_gamma_theta", "deterministic_forecast_min_cash_cents"):
        assert res[key] == pytest.approx(m[key], rel=1e-6), key
    assert res["worst_week_index"] == m["worst_week_index"]
    assert ["key_customer_timing", "weekly_billings", "collection_timing", "pipeline", "supplier_prepay"].index(res["top_driver"]) == m["top_driver_code"]
    if difficulty == "principal":
        assert res["pipeline_calibration_factor"] == pytest.approx(m["pipeline_calibration_factor"], rel=1e-6)


@pytest.mark.parametrize("difficulty", DIFFICULTIES)
@pytest.mark.parametrize("seed", SEEDS)
def test_plan_on_averages_hides_shortfall_risk(worlds: dict, difficulty: str, seed: int) -> None:
    world, folder = worlds[(difficulty, seed)]
    m = _metrics(world)
    a1 = world.ground_truth.anomalies[0]
    plan = _fetch(world, folder, "SELECT MIN(ending_cash_cents), COUNT(*) FROM management_forecast")[0]
    assert plan[1] == 13 and plan[0] > 0  # positive every week ...
    assert m["deterministic_forecast_min_cash_cents"] == plan[0]
    # ... while the simulation shows a material probability of running out (calibrated to the sampled target)
    target = a1.magnitude["p_shortfall_target"]
    lo, hi = {"foundation": (0.2925, 0.35), "practitioner": (0.20625, 0.26375), "principal": (0.12, 0.1775)}[difficulty]
    assert lo - 1e-9 <= target <= hi + 1e-9
    assert target - 0.15 <= m["p_shortfall_13w"] <= target + 0.03
    assert m["p_shortfall_13w"] > 0.04 and m["p_covenant_breach_13w"] >= m["p_shortfall_13w"]
    assert m["p5_min_cash_cents"] < 0 < m["expected_min_cash_cents"]
    assert m["top_driver_code"] == 0.0  # the key customer's payment timing dominates
    assert 1 <= m["worst_week_index"] <= 13


@pytest.mark.parametrize("difficulty", DIFFICULTIES)
def test_fitted_inputs_recover_sampled_parameters(worlds: dict, difficulty: str) -> None:
    for s in SEEDS:
        world, _ = worlds[(difficulty, s)]
        m = _metrics(world)
        a2 = next(a for a in world.ground_truth.anomalies if a.id == "A2")
        # Gamma by method of moments on floored days-late (floor shifts the mean by ~0.5 day)
        assert m["collection_delay_gamma_k"] == pytest.approx(a2.magnitude["late_gamma_k_param"], rel=0.25)
        assert m["collection_delay_gamma_theta"] == pytest.approx(a2.magnitude["late_gamma_theta_param"], rel=0.25)
        if difficulty == "principal":
            sampled = next(a for a in world.ground_truth.anomalies if a.id == "A4").magnitude["pipeline_calibration"]
            assert m["pipeline_calibration_factor"] == pytest.approx(sampled, abs=0.08)
        tail_q = next(q for q in a2.reference_sql if q.name == "key_customer_lateness_by_quarter")
        assert tail_q.expected["checks"]["quarters_with_episode"] == 2


def test_books_tie_and_cash_ledger_matches_gl(worlds: dict) -> None:
    for d in DIFFICULTIES:
        world, folder = worlds[(d, 2024)]
        assert all(c.passed for c in world.finance.invariants)
        ties = {c.name.split("[")[0] for c in world.finance.invariants if c.name.startswith("subledger:")}
        assert ties == {"subledger:ar_open_invoices", "subledger:ap_received_unpaid", "subledger:revenue_invoices"}
        (mn, last), = _fetch(world, folder, "SELECT MIN(closing_balance_cents), arg_max(closing_balance_cents, balance_date) FROM cash_ledger")
        assert mn >= 0  # "we've always been fine"
        assert last == world.finance.trial_balance[world.periods[-1]]["1000"]
        res = world.ground_truth.expected_metrics
        assert {x.key for x in res} >= {"p_shortfall_13w", "p_covenant_breach_13w", "expected_min_cash_cents", "p5_min_cash_cents",
                                         "worst_week_index", "top_driver_code", "collection_delay_gamma_k", "cash_needed_for_p95_safety_cents"}


def test_reference_runs_in_the_product_sandbox(worlds: dict) -> None:
    """Same code through the real Python sandbox (``python -I``, rlimits, read-only DB)."""
    from axiom.engine.sandbox import PythonSandbox

    world, folder = worlds[("practitioner", 1)]
    out = PythonSandbox(folder, timeout_s=120, memory_mb=1024, db_path=resolve_db_path(world, folder)).run(world.ground_truth.reference_solution.python[0])
    assert out["error"] is None, (out["error"], out["stderr"][-2000:])
    m = _metrics(world)
    assert out["result_json"]["p_shortfall_13w"] == pytest.approx(m["p_shortfall_13w"], abs=1e-3)


@pytest.mark.parametrize("difficulty", DIFFICULTIES)
@pytest.mark.parametrize("seed", SEEDS)
def test_gross_margin_moves_at_most_three_points_a_quarter(worlds: dict, difficulty: str, seed: int) -> None:
    """Review P1 #17: brokerage gross margin (carrier bills normalised per month + capped base GM noise) changes by
    <= 3 pts quarter to quarter and stays in the 3PL brokerage band."""
    world, _ = worlds[(difficulty, seed)]
    gm = [world.finance.statements.ratios[p]["gross_margin"] for p in world.periods]
    assert max(abs(b - a) for a, b in itertools.pairwise(gm)) <= 0.03
    assert all(0.10 <= g <= 0.30 for g in gm)


def test_restricted_to_asset_light_brokerage_without_inventory_exemption() -> None:
    """The inventory 'simplification' exemption is gone: only the asset-light 3PL brokerage (no stock) is offered."""
    from axiom.engine.archetypes.monte_carlo_cash import MonteCarloCash

    assert MonteCarloCash.industries == ("3pl",)
    assert not any("inventor" in k for k in MonteCarloCash.bs_caption_exemptions)
    with pytest.raises(ValueError):
        generate_world(KEY, 7, difficulty="foundation", industry="manufacturing", pool=get_default_pool())


def test_balance_sheet_has_no_stock(worlds: dict) -> None:
    world, _ = worlds[("practitioner", 7)]
    for bs in world.finance.statements.balance:
        caps = {ln.caption: ln.cents for ln in bs.lines}
        assert caps.get("Inventory, net", 0) == 0 and caps["Accounts receivable, net"] > 0 and caps["Accounts payable"] > 0
