"""Archetype registry.

``ARCHETYPE_REGISTRY`` maps archetype keys to classes. Modules listed in :data:`ARCHETYPE_MODULES`
are imported if present; a missing module (archetype not implemented yet) is skipped silently,
so :func:`catalog` lists only what is registered. A module registers its class with the
:func:`register` decorator.
"""

from __future__ import annotations

import importlib
import logging

from axiom.engine.archetypes.base import Archetype
from axiom.models import ArchetypeInfo

__all__ = ["ARCHETYPE_MODULES", "ARCHETYPE_REGISTRY", "register", "get_archetype", "catalog"]

logger = logging.getLogger("axiom.archetypes")

ARCHETYPE_REGISTRY: dict[str, type[Archetype]] = {}

# Spec order (docs/research/archetypes.md). Implementers add the module file; nothing else to edit.
ARCHETYPE_MODULES: tuple[str, ...] = (
    "working_capital_crunch",
    "abc_cost_distortion",
    "ma_synergies",
    "forensic_revenue",
    "toc_bottleneck",
    "cohort_collapse",
    "bullwhip",
    "pricing_elasticity",
    "idempotent_webhooks",
    "outbox_pattern",
    "replica_lag",
    "window_reconciliation",
    "cohort_matrix",
    "monte_carlo_cash",
)


def register(cls: type[Archetype]) -> type[Archetype]:
    """Class decorator: add ``cls`` to :data:`ARCHETYPE_REGISTRY` under ``cls.key``."""
    if cls.key in ARCHETYPE_REGISTRY and ARCHETYPE_REGISTRY[cls.key] is not cls:
        raise ValueError(f"archetype {cls.key!r} registered twice")
    ARCHETYPE_REGISTRY[cls.key] = cls
    return cls


def _load_all() -> None:
    for name in ARCHETYPE_MODULES:
        try:
            importlib.import_module(f"{__name__}.{name}")
        except ModuleNotFoundError as exc:
            if exc.name == f"{__name__}.{name}":
                logger.debug("archetype module %s not implemented yet", name)
                continue
            raise


def get_archetype(key: str) -> Archetype:
    """Instantiate the archetype registered under ``key`` (``KeyError`` if unknown)."""
    try:
        return ARCHETYPE_REGISTRY[key]()
    except KeyError:
        raise KeyError(f"unknown archetype {key!r}; registered: {sorted(ARCHETYPE_REGISTRY)}") from None


def catalog() -> list[ArchetypeInfo]:
    """``ArchetypeInfo`` for every registered archetype, in spec order."""
    order = {k: i for i, k in enumerate(ARCHETYPE_MODULES)}
    return [ARCHETYPE_REGISTRY[k].info() for k in sorted(ARCHETYPE_REGISTRY, key=lambda k: (order.get(k, 999), k))]


_load_all()
