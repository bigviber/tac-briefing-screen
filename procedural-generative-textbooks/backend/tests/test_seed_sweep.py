"""Seed sweep (slow): every archetype at foundation for seeds 0-29 must generate with every invariant passing.

Excluded by default (``-m 'not slow'`` in pyproject); run with ``python -m pytest -m slow tests/test_seed_sweep.py``.
Restrict archetypes with ``AXIOM_TEST_ARCHETYPES=a,b`` and seeds with ``AXIOM_SWEEP_SEEDS=0-29`` (range or list).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from axiom.engine.archetypes import ARCHETYPE_REGISTRY
from axiom.engine.market_research import get_default_pool
from axiom.engine.procedural_world import generate_world

pytestmark = pytest.mark.slow


def _seeds() -> list[int]:
    raw = os.environ.get("AXIOM_SWEEP_SEEDS", "0-29").strip()
    if "-" in raw and "," not in raw:
        lo, hi = (int(x) for x in raw.split("-", 1))
        return list(range(lo, hi + 1))
    return [int(x) for x in raw.split(",") if x.strip()]


def _archetypes() -> list[str]:
    raw = os.environ.get("AXIOM_TEST_ARCHETYPES", "").strip()
    keys = [k.strip() for k in raw.split(",") if k.strip()] if raw else sorted(ARCHETYPE_REGISTRY)
    unknown = sorted(set(keys) - set(ARCHETYPE_REGISTRY))
    if unknown:
        raise pytest.UsageError(f"unknown archetypes {unknown}")
    return keys


@pytest.mark.parametrize("seed", _seeds())
@pytest.mark.parametrize("archetype", _archetypes())
def test_foundation_seed_sweep(tmp_path: Path, archetype: str, seed: int) -> None:
    world = generate_world(archetype, seed, difficulty="foundation", out_dir=tmp_path, pool=get_default_pool())
    failed = [c for c in world.finance.invariants if not c.passed]
    assert not failed, [f"{c.name}: {c.detail}" for c in failed]
    assert world.company.headcount >= 8 and world.company.revenue_ttm_cents > 0
