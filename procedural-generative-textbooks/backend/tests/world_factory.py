"""Small synthetic ``CaseWorld`` builder for Module 3 tests (no dependency on Module 2).

``make_minimal_world("working_capital_crunch")`` satisfies the models with 2 periods, 3
accounts, 1 anomaly, 3 expected metrics, 2 levers, a dossier seed with reveal/conceal and a
simulation ``model`` dict in the format documented on
:func:`axiom.engine.pedagogy.simulate_transition`. ``forensic_revenue`` and ``outbox_pattern``
variants carry the metric keys used by the pedagogy fixtures.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from axiom.models import (
    Account,
    Anomaly,
    BalanceSheet,
    CaseWorld,
    CashFlowStatement,
    ColumnSpec,
    CompanyProfile,
    DatabaseSpec,
    DossierSeed,
    ExpectedMetric,
    FinanceState,
    FinancialStatements,
    GroundTruth,
    IncomeStatement,
    IndustryProfile,
    Lever,
    OperationsState,
    ReferenceQuery,
    ReferenceSolution,
    SimulationState,
    Stakeholder,
    TableSpec,
    TimelineEvent,
)

DAG = json.loads((Path(__file__).resolve().parents[1] / "axiom" / "skill_dag.json").read_text(encoding="utf-8"))
FIXTURES_DIR = Path(__file__).resolve().parents[1] / "axiom" / "engine" / "pedagogy_fixtures"

PERIODS = ["2024Q3", "2024Q4"]


def load_fixture(name: str) -> dict[str, Any]:
    """Load a pedagogy fixture by file stem."""
    return json.loads((FIXTURES_DIR / f"{name}.json").read_text(encoding="utf-8"))


def _pillars_of(nodes: list[str]) -> list[str]:
    pillar = {n["id"]: n["pillar"] for n in DAG["nodes"]}
    return list(dict.fromkeys(pillar[n] for n in nodes))


def _finance() -> FinanceState:
    accounts = [
        Account(code="1000", name="Cash", type="asset", normal_balance="debit", statement="BS", section="current_assets"),
        Account(code="1100", name="Accounts Receivable", type="asset", normal_balance="debit", statement="BS", section="current_assets"),
        Account(code="4000", name="Revenue", type="revenue", normal_balance="credit", statement="IS", section="revenue"),
    ]
    income = [
        IncomeStatement(period="2024Q3", revenue_cents=1_930_000_000, gross_profit_cents=810_600_000, operating_income_cents=150_000_000, net_income_cents=110_000_000),
        IncomeStatement(period="2024Q4", revenue_cents=2_100_000_000, gross_profit_cents=861_000_000, operating_income_cents=160_000_000, net_income_cents=118_000_000),
    ]
    balance = [
        BalanceSheet(period="2024Q3", total_assets_cents=5_000_000_000, total_liabilities_cents=3_000_000_000, total_equity_cents=2_000_000_000),
        BalanceSheet(period="2024Q4", total_assets_cents=5_400_000_000, total_liabilities_cents=3_282_000_000, total_equity_cents=2_118_000_000),
    ]
    cash = [
        CashFlowStatement(period="2024Q3", operating_cents=-40_000_000, investing_cents=-10_000_000, financing_cents=0, net_change_cents=-50_000_000, beginning_cash_cents=300_000_000, ending_cash_cents=250_000_000),
        CashFlowStatement(period="2024Q4", operating_cents=-130_000_000, investing_cents=0, financing_cents=0, net_change_cents=-130_000_000, beginning_cash_cents=250_000_000, ending_cash_cents=120_000_000),
    ]
    ratios = {"2024Q3": {"dso": 59.8, "dio": 37.0, "dpo": 40.0}, "2024Q4": {"dso": 68.1, "dio": 38.0, "dpo": 41.0}}
    return FinanceState(
        chart_of_accounts=accounts,
        statements=FinancialStatements(periods=list(PERIODS), income=income, balance=balance, cash_flow=cash, ratios=ratios),
    )


def _database() -> DatabaseSpec:
    invoices = TableSpec(
        name="invoices",
        ddl="CREATE TABLE invoices (invoice_id VARCHAR, customer_id VARCHAR, issued_at VARCHAR, amount_cents BIGINT, terms_days INTEGER, paid_at VARCHAR, updated_at VARCHAR)",
        columns=[
            ColumnSpec(name="invoice_id", type="VARCHAR", nullable=False, description="invoice id (duplicated on re-sync)"),
            ColumnSpec(name="customer_id", type="VARCHAR", description="customer"),
            ColumnSpec(name="issued_at", type="VARCHAR", description="issue timestamp, mixed time zones"),
            ColumnSpec(name="amount_cents", type="BIGINT", description="invoice amount in cents"),
            ColumnSpec(name="terms_days", type="INTEGER", description="payment terms"),
            ColumnSpec(name="paid_at", type="VARCHAR", description="payment timestamp or null"),
            ColumnSpec(name="updated_at", type="VARCHAR", description="last update"),
        ],
        row_count=4200,
        primary_key=["invoice_id"],
        dirt=["dup_keys:0.02", "tz_mixed:issued_at"],
        sample_rows=[{"invoice_id": "INV-1", "customer_id": "C-7", "issued_at": "2024-07-01T10:00:00Z", "amount_cents": 125000, "terms_days": 45, "paid_at": None, "updated_at": "2024-07-02"}],
    )
    customers = TableSpec(
        name="customers", ddl="CREATE TABLE customers (customer_id VARCHAR, name VARCHAR, segment VARCHAR)",
        columns=[ColumnSpec(name="customer_id", type="VARCHAR"), ColumnSpec(name="name", type="VARCHAR"), ColumnSpec(name="segment", type="VARCHAR")],
        row_count=310, primary_key=["customer_id"],
    )
    return DatabaseSpec(path="cases/test/company.duckdb", tables=[invoices, customers], erd_mermaid="erDiagram\n  customers ||--o{ invoices : bills", total_rows=4510)


def _simulation_model() -> dict[str, Any]:
    return {
        "kpis": {"dso": 68.0, "cash_cents": 120_000_000.0, "revenue_cents": 2_100_000_000.0},
        "lever_effects": [
            {"lever": "terms_days", "kpi": "dso", "delta_per_unit": 0.6, "lag_periods": 0},
            {"lever": "terms_days", "kpi": "revenue_cents", "elasticity": 0.05, "lag_periods": 1},
            {"lever": "early_pay_discount_pct", "kpi": "dso", "delta_per_unit": -300.0, "lag_periods": 0},
        ],
        "anomaly_drag": {"cash_cents": -25_000_000.0},
        "resolved_if": {"terms_days": "<=45"},
        "bounds": {"dso": [0, 365]},
    }


def _levers() -> list[Lever]:
    return [
        Lever(key="terms_days", label="Payment terms for new invoices", min=30, max=90, default=90, unit="days", effect="shorter terms pull cash forward but cost some volume"),
        Lever(key="early_pay_discount_pct", label="Early-payment discount", min=0.0, max=0.03, default=0.0, unit="ratio", effect="discount accelerates collections at a margin cost"),
    ]


def _ground_truth(archetype: str) -> GroundTruth:
    if archetype == "forensic_revenue":
        anomaly = Anomaly(
            id="A1", kind="channel_stuffing", pillar="accounting_finance", title="Quarter-end channel stuffing",
            mechanism="late-quarter shipments with extended terms and side-letter returns", periods=["2024Q4"],
            affected_accounts=["4000", "1100"], affected_tables=["invoices"], magnitude={"revenue_inflation_pct": 0.11},
            evidence_trail=["Q4 last-two-week shipments spike", "returns reverse in the next quarter", "terms_days lengthen on late invoices"],
            reference_sql=[ReferenceQuery(name="cutoff_spike", sql="SELECT ship_date, LAG(qty) OVER (PARTITION BY sku ORDER BY ship_date) FROM shipments",
                                          expected={"signature": ["lag(", "partition by", "ship_date"]})],
            severity="critical",
        )
        metrics = [
            ExpectedMetric(key="revenue_growth_q4_pct", value=0.12, unit="ratio", tolerance_abs=0.01, derivation="dedup invoices, Q4/Q3 - 1"),
            ExpectedMetric(key="q4_revenue_cents", value=1_830_000_000, unit="cents", tolerance_rel=0.02, derivation="sum of deduplicated Q4 invoices"),
            ExpectedMetric(key="returns_rate_q1_next", value=0.09, unit="ratio", tolerance_abs=0.01, derivation="returns / prior-quarter revenue"),
        ]
        return GroundTruth(
            root_cause="Quarter-end channel stuffing: shipments pulled into Q4 under extended terms with side-letter returns.",
            anomalies=[anomaly], red_herrings=["Q4 seasonality and the new enterprise sales team explain the growth"],
            expected_metrics=metrics, reference_solution=ReferenceSolution(sql=[anomaly.reference_sql[0].sql]), decision_levers=_levers(),
        )
    if archetype == "outbox_pattern":
        a1 = Anomaly(
            id="A1", kind="dual_write_loss", pillar="system_design", title="Dual-write drops billing events",
            mechanism="usage row committed, broker publish non-transactional", periods=list(PERIODS), affected_tables=["usage_events", "invoice_lines"],
            magnitude={"unbilled_usage_rate": 0.038, "gap_rate_incident_days": 0.10, "gap_rate_baseline": 0.003},
            evidence_trail=["gaps cluster on deploy days", "consumer DLQ empty"],
            reference_sql=[ReferenceQuery(name="unbilled_usage", sql="SELECT ... FULL OUTER JOIN ...",
                                          expected={"signature": ["full outer join", "row_number()", "partition by usage_id"]})],
            severity="high",
        )
        a2 = Anomaly(id="A2", kind="unbilled_revenue", pillar="accounting_finance", title="Unbilled usage revenue",
                     mechanism="earned but uninvoiced usage", magnitude={"unbilled_usage_cents": 300_000_000}, severity="medium")
        metrics = [
            ExpectedMetric(key="unbilled_usage_rate", value=0.038, unit="ratio", tolerance_abs=0.005),
            ExpectedMetric(key="unbilled_usage_cents", value=300_000_000, unit="cents", tolerance_rel=0.05),
            ExpectedMetric(key="gap_rate_incident_days", value=0.10, unit="ratio", tolerance_abs=0.02),
            ExpectedMetric(key="gap_rate_baseline", value=0.003, unit="ratio", tolerance_abs=0.002),
            ExpectedMetric(key="dlq_depth", value=0, unit="count", tolerance_abs=0),
        ]
        return GroundTruth(
            root_cause="Non-transactional dual write between the usage table and the broker loses billing events.",
            anomalies=[a1, a2], red_herrings=["the invoicing consumer is dropping messages"], expected_metrics=metrics,
            reference_solution=ReferenceSolution(design_mermaid="flowchart LR\n A-->B"), decision_levers=_levers(),
        )
    anomaly = Anomaly(
        id="A1", kind="terms_extension", pillar="accounting_finance", title="Payment terms extended to 90 days",
        mechanism="top-20 accounts moved to 90-day terms in 2024Q3", periods=list(PERIODS), affected_accounts=["1100", "1000"],
        affected_tables=["invoices"], magnitude={"dso_delta_days": 27.0},
        evidence_trail=["DSO steps up in 2024Q3", "share of open AR on 90-day terms jumps"],
        reference_sql=[ReferenceQuery(
            name="dso_by_period",
            sql="WITH inv AS (SELECT *, ROW_NUMBER() OVER (PARTITION BY invoice_id ORDER BY updated_at DESC) rn FROM invoices) SELECT ...",
            expected={"signature": ["row_number()", "partition by invoice_id", "paid_at is null"]},
        )],
        severity="high",
    )
    metrics = [
        ExpectedMetric(key="dso_latest", value=68.0, unit="days", tolerance_abs=2.0, derivation="open AR / revenue x 91"),
        ExpectedMetric(key="dso_baseline", value=41.0, unit="days", tolerance_abs=2.0, derivation="2024Q1 DSO"),
        ExpectedMetric(key="cash_trapped_in_ar_cents", value=570_000_000, unit="cents", tolerance_rel=0.05, derivation="revenue per day x DSO delta"),
    ]
    return GroundTruth(
        root_cause="The 2024Q3 move to 90-day terms for the top-20 accounts pushed DSO up 27 days and trapped cash in receivables.",
        anomalies=[anomaly],
        red_herrings=["inventory overstocking is consuming cash", "demand is softening"],
        expected_metrics=metrics,
        reference_solution=ReferenceSolution(sql=[anomaly.reference_sql[0].sql], memo_bullets=["revert terms", "bridge with revolver"]),
        decision_levers=_levers(),
    )


def make_minimal_world(archetype: str = "working_capital_crunch", *, seed: int = 1, difficulty: str = "practitioner") -> CaseWorld:
    """A valid synthetic ``CaseWorld`` for ``archetype`` (any DAG archetype key)."""
    nodes = list(DAG["archetype_nodes"].get(archetype, DAG["archetype_nodes"]["working_capital_crunch"]))
    return CaseWorld(
        case_id=f"case_{archetype}_{seed}",
        seed=seed,
        archetype=archetype,
        version="test-1",
        pillars=_pillars_of(nodes),  # type: ignore[arg-type]
        skill_nodes=nodes,
        difficulty=difficulty,  # type: ignore[arg-type]
        company=CompanyProfile(
            name="Kestrel Supply Co.", ticker_like="KSC", industry_code="ecommerce", business_model="b2b_distribution",
            hq="Columbus, OH", founded=2011, headcount=240, stage="growth", revenue_ttm_cents=7_900_000_000,
            description="Regional B2B distributor of packaging supplies.",
        ),
        industry=IndustryProfile(code="ecommerce", name="E-commerce / B2B distribution", narrative_hooks=["freight costs", "large retail customers negotiate terms"]),
        periods=list(PERIODS),
        finance=_finance(),
        operations=OperationsState(kpis_by_period={"2024Q3": {"orders": 41200.0, "gross_margin": 0.42}, "2024Q4": {"orders": 44900.0, "gross_margin": 0.41}}),
        database=_database(),
        simulation=SimulationState(current_period="2024Q4", history=[], model=_simulation_model()),
        ground_truth=_ground_truth(archetype),
        dossier_seed=DossierSeed(
            reveal=["cash fell to $1.2M at the end of 2024Q4 despite revenue growth", "the revolver covenant requires minimum liquidity"],
            conceal=["the 2024Q3 move to 90-day payment terms for the top-20 accounts", "A1"],
            stakeholders=[
                Stakeholder(name="Dana Okafor", role="CFO", agenda="avoid a covenant breach", disposition="anxious",
                            knows=["cash is tight"], hides=["she approved the terms change"]),
                Stakeholder(name="Luis Ortiz", role="COO", agenda="defend inventory levels", disposition="defensive",
                            knows=["inventory is flat"], hides=[]),
            ],
            timeline=[
                TimelineEvent(period="2024Q3", headline="New ERP go-live", detail="invoicing moved to the new ERP"),
                TimelineEvent(period="2024Q4", headline="Warehouse expansion", detail="COO adds a second shift", is_red_herring=True),
            ],
            artifacts=["board_deck_excerpt", "slack_thread"],
        ),
        generated_at="2026-01-01T00:00:00+00:00",
    )
