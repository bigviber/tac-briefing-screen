"""Shared pytest fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from axiom.engine.market_research import BenchmarkPool

FIXTURE_BENCHMARKS = Path(__file__).resolve().parent / "fixtures" / "benchmarks"


@pytest.fixture(scope="session")
def fixture_benchmarks_dir() -> Path:
    """Directory of the mini research corpus used by tests."""
    return FIXTURE_BENCHMARKS


@pytest.fixture()
def pool(fixture_benchmarks_dir: Path) -> BenchmarkPool:
    """A fresh pool loaded from the fixture corpus."""
    return BenchmarkPool.load(fixture_benchmarks_dir)
