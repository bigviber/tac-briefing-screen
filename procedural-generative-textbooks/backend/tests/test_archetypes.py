"""Every registered archetype x difficulty x seed: invariants, ground truth, dossier, simulation, determinism."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import duckdb
import pytest

from axiom.engine.archetypes import ARCHETYPE_MODULES, ARCHETYPE_REGISTRY, catalog
from axiom.engine.archetypes.base import SIM_MODEL_KEYS, satisfying_decisions, sql_signature
from axiom.engine.market_research import get_default_pool
from axiom.engine.procedural_world import generate_world, load_world, make_case_id, resolve_db_path, world_json_bytes
from axiom.models import CaseWorld


def _selected_archetypes() -> list[str]:
    """Registry keys, restricted by ``AXIOM_TEST_ARCHETYPES="a,b"`` when set (unknown keys are an error)."""
    raw = os.environ.get("AXIOM_TEST_ARCHETYPES", "").strip()
    if not raw:
        return sorted(ARCHETYPE_REGISTRY)
    wanted = [k.strip() for k in raw.split(",") if k.strip()]
    unknown = sorted(set(wanted) - set(ARCHETYPE_REGISTRY))
    if unknown:
        raise pytest.UsageError(f"AXIOM_TEST_ARCHETYPES names unknown archetypes {unknown}; known: {sorted(ARCHETYPE_REGISTRY)}")
    return sorted(set(wanted))


# NumPy >= 2.5 deprecates generic-unit timedeltas (bare ints added to datetime64); generation must stay clean.
pytestmark = pytest.mark.filterwarnings("error:The 'generic' unit for NumPy timedelta:DeprecationWarning")

ARCHETYPES = _selected_archetypes()
DIFFICULTIES = ("foundation", "practitioner", "principal")
SEEDS = (1, 7, 2024)


class WorldGenerationError(RuntimeError):
    """A cached (archetype, difficulty, seed) generation failed earlier in this module."""


class WorldCache(dict):
    """``cache[(archetype, difficulty, seed)] -> (world, folder)``, generated lazily on first access and
    memoised for the module. A failed generation is memoised too, so only the tests that ask for that
    world fail (each re-raises it) while every other archetype's tests run normally."""

    def __init__(self, root: Path) -> None:
        super().__init__()
        self.root = root
        self.pool = get_default_pool()
        self.errors: dict[tuple[str, str, int], BaseException] = {}

    def __missing__(self, key: tuple[str, str, int]) -> tuple[CaseWorld, Path]:
        if key in self.errors:
            raise WorldGenerationError(f"generation of {key} failed earlier in this module") from self.errors[key]
        a, d, s = key
        folder = self.root / f"{a}-{d}-{s}"
        try:
            value = (generate_world(a, s, difficulty=d, out_dir=folder, pool=self.pool), folder)
        except Exception as exc:
            self.errors[key] = exc
            raise
        self[key] = value
        return value


@pytest.fixture(scope="module")
def worlds(tmp_path_factory: pytest.TempPathFactory) -> WorldCache:
    """Per-(archetype, difficulty, seed) world cache shared by the module (lazy, failure-isolated)."""
    return WorldCache(tmp_path_factory.mktemp("worlds"))


def _require(archetype: str) -> None:
    if archetype not in ARCHETYPES:
        pytest.skip(f"{archetype} not in AXIOM_TEST_ARCHETYPES")


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
    # 2 / 4 / 6 designed rules; principal adds extra realism dirt on columns the answer key never reads (review C8)
    assert len(applied) == {"foundation": 2, "practitioner": 4}[difficulty] if difficulty != "principal" else len(applied) >= 6
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
    _require("working_capital_crunch")
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
    _require("idempotent_webhooks")
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


@pytest.mark.parametrize("archetype", ARCHETYPES)
def test_simulation_model_runs_in_quarter_simulator(worlds: dict, archetype: str) -> None:
    """Integration with Module 3's simulator when it is importable (it is built concurrently)."""
    try:
        from axiom.engine.pedagogy import simulate_transition
    except Exception as exc:  # pragma: no cover - depends on Module 3 state
        pytest.skip(f"pedagogy not importable: {exc}")
    world, _ = worlds[(archetype, "practitioner", 1)]
    levers = world.ground_truth.decision_levers
    decisions = satisfying_decisions(world.simulation.model, levers)  # max for ">=" conditions, min for "<="
    q, info = simulate_transition(world.simulation, levers, decisions)
    assert info["resolved"] and set(world.simulation.model["kpis"]) <= set(q.kpis)
    status_quo, info0 = simulate_transition(world.simulation, levers, {})
    assert not info0["resolved"]
    for kpi, (lo, hi) in (world.simulation.model.get("bounds") or {}).items():  # bounds hold after a transition
        for quarter in (q, status_quo):
            assert (lo is None or quarter.kpis[kpi] >= lo) and (hi is None or quarter.kpis[kpi] <= hi), (kpi, quarter.kpis[kpi])
    json.dumps(q.model_dump(mode="json"))


def test_load_world_from_cases_dir(tmp_path: Path) -> None:
    _require("idempotent_webhooks")
    world = generate_world("idempotent_webhooks", 3, difficulty="foundation", out_dir=tmp_path / make_case_id("idempotent_webhooks", 3, "foundation"))
    loaded = load_world(world.case_id, cases_dir=tmp_path)
    assert loaded == world and loaded.generated_at == world.generated_at
    with pytest.raises(FileNotFoundError):
        load_world("nope", cases_dir=tmp_path)


# --------------------------------------------------------------------------- mermaid syntax (QA F1)

_MERMAID_SHAPES = (  # (open, close) of node shapes, longest first
    ("[(", ")]"), ("((", "))"), ("{{", "}}"), ("[[", "]]"), ("([", "])"), ("[/", "/]"), ("[\\", "\\]"),
    ("[", "]"), ("(", ")"), ("{", "}"), (">", "]"),
)
_SPECIAL = set("()[]{}|\"")


def mermaid_syntax_errors(src: str) -> list[str]:
    """A conservative Mermaid flowchart lint: quoted strings are opaque; an unquoted node label or ``|edge label|``
    containing parentheses, brackets, braces, pipes or quotes is rejected (Mermaid 11 fails to parse it, QA F1)."""
    import re

    errors: list[str] = []
    lines = src.splitlines()
    if not lines or not re.match(r"^(flowchart|graph)\s+(LR|RL|TD|TB|BT)\s*$", lines[0].strip()):
        errors.append(f"first line must be 'flowchart <dir>': {lines[:1]}")
    for n, raw in enumerate(lines[1:], start=2):
        line = raw.strip()
        if not line or line.startswith(("%%", "classDef ", "class ", "style ", "linkStyle ", "subgraph", "end", "direction ")):
            continue
        if line.count('"') % 2:
            errors.append(f"line {n}: unbalanced quotes: {line}")
            continue
        s = re.sub(r'"[^"]*"', '""', line)  # quoted text is opaque to the parser
        for m in re.finditer(r"\|([^|]*)\|", s):  # unquoted edge labels
            if set(m.group(1)) & (_SPECIAL - {'"'}):
                errors.append(f"line {n}: edge label needs quotes: {m.group(0)}")
        s = re.sub(r"\|[^|]*\|", "", s)
        i = 0
        while i < len(s):
            m = re.match(r"[A-Za-z_][\w-]*", s[i:])
            if not m or (i > 0 and (s[i - 1].isalnum() or s[i - 1] == "_")):
                i += 1
                continue
            j = i + m.end()
            shape = next(((o, c) for o, c in _MERMAID_SHAPES if s.startswith(o, j)), None)
            if shape is None:
                i = j
                continue
            o, c = shape
            k = s.find(c, j + len(o))
            if k < 0:
                errors.append(f"line {n}: unclosed node shape after {m.group(0)}: {line}")
                break
            label = s[j + len(o): k]
            if label != '""' and set(label) & _SPECIAL:
                errors.append(f"line {n}: node {m.group(0)} label needs quotes: {o}{label}{c}")
            i = k + len(c)
    return errors


def test_mermaid_lint_rejects_unquoted_specials() -> None:
    assert mermaid_syntax_errors('flowchart LR\n  A["x (y)"] -->|"a (b)"| DB[(Postgres)]\n  DB --> C{"ok?"}') == []
    assert mermaid_syntax_errors("flowchart LR\n  F[Finance Recon job (monthly)] --> X")
    assert mermaid_syntax_errors("flowchart LR\n  CDN[Availability cache TTL n/a (browse pages only)]")
    assert mermaid_syntax_errors("flowchart LR\n  A -->|a (b)| B")
    assert mermaid_syntax_errors("flowchart LR\n  A[x | y] --> B")


@pytest.mark.parametrize("archetype", ARCHETYPES)
@pytest.mark.parametrize("difficulty", DIFFICULTIES)
def test_mermaid_diagrams_parse(worlds: dict, archetype: str, difficulty: str) -> None:
    """QA F1: every architecture / design / process-flow diagram passes the syntax lint."""
    world, _ = worlds[(archetype, difficulty, 7)]
    diagrams = {"architecture_mermaid": world.dossier_seed.architecture_mermaid}
    ref = getattr(world.ground_truth, "reference_solution", None)
    if ref is not None:
        diagrams["design_mermaid"] = ref.design_mermaid
    for name, src in diagrams.items():
        if src:
            assert mermaid_syntax_errors(src) == [], (name, src)


def test_bs_caption_invariant_flags_missing_lines() -> None:
    """Review C1: a SaaS balance sheet without AP / deferred revenue fails the (required) caption check."""
    from axiom.engine.archetypes.base import bs_caption_checks
    from axiom.engine.finance import FiscalCalendar, Ledger, build_statements

    cal = FiscalCalendar.from_seed(1)
    L = Ledger()
    L.open_books(cal.opening_date, {"1000": 10_000_00, "1100": 2_000_00, "1300": 100_00, "1500": 500_00, "2100": -300_00, "3000": -12_300_00})
    st = build_statements(L, cal.periods)
    checks = {c.name: c for c in bs_caption_checks(get_default_pool(), "saas", st)}
    assert all(c.required for c in checks.values())
    assert not checks["bs_caption_present[Accounts payable]"].passed
    assert not checks["bs_caption_present[Deferred revenue (current + non-current)]"].passed
    assert checks["bs_caption_present[Accounts receivable, net of allowance]"].passed
    assert checks["bs_caption_present[Goodwill and intangibles]"].passed  # exempt, with its reason
    assert "exempt" in checks["bs_caption_present[Goodwill and intangibles]"].detail
    exempted = bs_caption_checks(get_default_pool(), "saas", st, exemptions={"saas:accounts payable": "test"})
    assert next(c for c in exempted if c.name == "bs_caption_present[Accounts payable]").passed


@pytest.mark.parametrize("archetype", ARCHETYPES)
def test_company_profile_realism(worlds: dict, archetype: str) -> None:
    """Review C4: stage from the industry mix, FYE drawn, headcount from revenue per employee, founding era, casing,
    and no customer named after the company."""
    from axiom.engine.archetypes.base import founded_floor

    for seed in SEEDS:
        world, folder = worlds[(archetype, "practitioner", seed)]
        c = world.company
        assert c.stage in world.industry.stage_mix and world.industry.stage_mix[c.stage] > 0 or c.stage == "growth"
        assert c.fiscal_year_end in ("12-31", "06-30", "09-30", "03-31")
        per_head = c.revenue_ttm_cents / 100 / c.headcount
        assert c.headcount >= 8 and (c.headcount == 8 or 100_000 <= per_head <= 800_000), (c.headcount, per_head)
        floor = founded_floor(c.business_model, c.industry_code)
        assert floor is None or c.founded >= floor
        assert c.description[:1] == c.description[:1].upper() and "Dtc " not in c.description and " b2b" not in c.description
        stem = c.name.split()[0]
        con = duckdb.connect(str(resolve_db_path(world, folder)), read_only=True)
        try:
            for t in ("customers", "suppliers"):
                if t in {r[0] for r in con.execute("SHOW TABLES").fetchall()}:
                    cols = {r[0] for r in con.execute(f"DESCRIBE {t}").fetchall()}
                    if "name" in cols:
                        hits = con.execute(f"SELECT COUNT(*) FROM {t} WHERE lower(trim(name)) LIKE ?", [stem.lower() + " %"]).fetchone()[0]
                        assert hits == 0, (t, stem)
        finally:
            con.close()
