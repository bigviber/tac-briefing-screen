"""working_capital_crunch: the dossier conceals the finding (not the vocabulary) and carries an order-to-cash process map."""

from __future__ import annotations

import pytest

from axiom.engine.market_research import get_default_pool
from axiom.engine.procedural_world import generate_world
from axiom.models import CaseWorld
from tests.test_archetypes import mermaid_syntax_errors

KEY = "working_capital_crunch"
DIFFICULTIES = ("foundation", "practitioner", "principal")
SEEDS = (1, 7)


@pytest.fixture(scope="module")
def worlds(tmp_path_factory: pytest.TempPathFactory) -> dict[tuple[str, int], CaseWorld]:
    pool = get_default_pool()
    root = tmp_path_factory.mktemp("wcc")
    return {(d, s): generate_world(KEY, s, difficulty=d, out_dir=root / f"{d}-{s}", pool=pool) for d in DIFFICULTIES for s in SEEDS}


@pytest.mark.parametrize("difficulty", DIFFICULTIES)
def test_conceal_targets_the_finding_not_the_term(worlds: dict, difficulty: str) -> None:
    """Review P2 #25: 'cash conversion cycle' is CFO vocabulary; the conceal list hides what happened to it."""
    for seed in SEEDS:
        world = worlds[(difficulty, seed)]
        conceal = world.dossier_seed.conceal
        assert "cash conversion cycle" not in [c.lower() for c in conceal]  # the bare term is not concealed
        m = {x.key: x.value for x in world.ground_truth.expected_metrics}
        delta = f"{m['ccc_delta_p5_to_p8_days']:.0f} days"
        assert any(c.startswith("the cash conversion cycle lengthened") and delta in c for c in conceal), conceal
        assert any(c.startswith("CCC lengthened") for c in conceal)
        assert any(c.startswith("terms extended to") and "top accounts" in c for c in conceal)
        assert m["ccc_delta_p5_to_p8_days"] > 0


@pytest.mark.parametrize("difficulty", DIFFICULTIES)
def test_order_to_cash_process_map(worlds: dict, difficulty: str) -> None:
    """Review P2 #26: an order-to-cash Mermaid map ships with the dossier and passes the syntax lint."""
    world = worlds[(difficulty, 7)]
    src = world.dossier_seed.architecture_mermaid
    assert src and src.startswith("flowchart LR")
    assert mermaid_syntax_errors(src) == []
    for table in ("orders", "shipments", "invoices", "payments", "purchase_orders"):
        assert f"({table}" in src or f", {table}" in src
    assert "order_to_cash_process_map" in world.dossier_seed.artifacts
