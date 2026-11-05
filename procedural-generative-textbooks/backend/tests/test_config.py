"""Tests for axiom.config."""

from __future__ import annotations

from pathlib import Path

from axiom.config import PACKAGE_DIR, Settings, load_env_overrides


def test_defaults_and_derived_paths(tmp_path: Path) -> None:
    s = Settings(data_dir=tmp_path / "d")
    assert s.cases_dir == (tmp_path / "d" / "cases").resolve()
    assert s.app_db_path == (tmp_path / "d" / "axiom.db").resolve()
    assert s.benchmarks_dir == PACKAGE_DIR / "engine" / "benchmarks"
    assert (s.sandbox_timeout_s, s.sandbox_memory_mb, s.sql_row_limit) == (20, 512, 500)
    assert s.cors_origins == ["http://localhost:5173"]
    s.ensure_dirs()
    assert s.cases_dir.is_dir() and s.research_cache_dir is not None and s.research_cache_dir.is_dir()


def test_env_overrides(tmp_path: Path) -> None:
    env = {
        "AXIOM_DATA_DIR": str(tmp_path),
        "AXIOM_SQL_ROW_LIMIT": "50",
        "AXIOM_CORS_ORIGINS": "http://a:1, http://b:2",
        "AXIOM_UNKNOWN": "x",
        "OTHER": "y",
    }
    overrides = load_env_overrides(env)
    assert "unknown" not in overrides and "other" not in overrides
    s = Settings(**overrides)
    assert s.sql_row_limit == 50 and s.data_dir == tmp_path.resolve()
    assert s.cors_origins == ["http://a:1", "http://b:2"]
    assert Settings(cors_origins='["http://c:3"]').cors_origins == ["http://c:3"]
