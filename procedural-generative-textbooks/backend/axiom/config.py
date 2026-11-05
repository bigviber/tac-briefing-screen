"""Runtime configuration for Axiom.

Settings are a plain pydantic model populated from environment variables carrying the
``AXIOM_`` prefix (e.g. ``AXIOM_DATA_DIR``, ``AXIOM_SQL_ROW_LIMIT``). Paths that are not set
explicitly are derived from ``data_dir``. No secrets are stored here.
"""

from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Any
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = ["BACKEND_DIR", "PACKAGE_DIR", "Settings", "get_settings", "load_env_overrides"]

logger = logging.getLogger("axiom.config")

PACKAGE_DIR: Path = Path(__file__).resolve().parent
BACKEND_DIR: Path = PACKAGE_DIR.parent
ENV_PREFIX = "AXIOM_"


class Settings(BaseModel):
    """Process-wide configuration. Derived paths are filled in by a model validator."""

    model_config = ConfigDict(extra="ignore", validate_assignment=False)

    data_dir: Path = Field(default_factory=lambda: BACKEND_DIR / "data")
    cases_dir: Path | None = None
    app_db_path: Path | None = None
    research_cache_dir: Path | None = None
    benchmarks_dir: Path = Field(default_factory=lambda: PACKAGE_DIR / "engine" / "benchmarks")
    sandbox_timeout_s: int = 20
    sandbox_memory_mb: int = 512
    sql_row_limit: int = 500
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])
    sec_user_agent: str = "Axiom local research tool contact@example.com"

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: Any) -> Any:
        """Accept a JSON list or a comma-separated string for CORS origins."""
        if isinstance(value, str):
            text = value.strip()
            if text.startswith("["):
                return json.loads(text)
            return [part.strip() for part in text.split(",") if part.strip()]
        return value

    @model_validator(mode="after")
    def _derive_paths(self) -> Settings:
        """Fill path defaults that depend on ``data_dir``."""
        self.data_dir = Path(self.data_dir).expanduser().resolve()
        if self.cases_dir is None:
            self.cases_dir = self.data_dir / "cases"
        if self.app_db_path is None:
            self.app_db_path = self.data_dir / "axiom.db"
        if self.research_cache_dir is None:
            self.research_cache_dir = self.data_dir / "research_cache"
        return self

    def ensure_dirs(self) -> None:
        """Create runtime directories if they do not exist yet."""
        assert self.cases_dir is not None and self.app_db_path is not None
        assert self.research_cache_dir is not None
        for directory in (self.data_dir, self.cases_dir, self.app_db_path.parent, self.research_cache_dir):
            Path(directory).mkdir(parents=True, exist_ok=True)


def load_env_overrides(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Collect ``AXIOM_*`` variables that correspond to ``Settings`` fields (lower-cased, prefix stripped)."""
    env = os.environ if environ is None else environ
    fields = set(Settings.model_fields)
    overrides: dict[str, str] = {}
    for name, value in env.items():
        if not name.startswith(ENV_PREFIX):
            continue
        field = name[len(ENV_PREFIX):].lower()
        if field in fields:
            overrides[field] = value
    return overrides


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached process settings, creating runtime directories on first access."""
    settings = Settings(**load_env_overrides())
    settings.ensure_dirs()
    logger.debug("settings loaded: data_dir=%s", settings.data_dir)
    return settings
