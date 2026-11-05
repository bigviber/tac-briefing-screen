"""HTTP API (FastAPI routers mounted at ``/api/v1``; ARCHITECTURE.md sections 5, 11 and 12).

Routers are imported by :func:`build_api_router` (not at package import) so that
:mod:`axiom.api.schemas` can be used by :mod:`axiom.services` without an import cycle.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import APIRouter

__all__ = ["API_PREFIX", "build_api_router"]

API_PREFIX = "/api/v1"


def build_api_router() -> APIRouter:
    """One router holding every endpoint (without the ``/api/v1`` prefix)."""
    from fastapi import APIRouter

    from axiom.api import routes_archetypes, routes_cases, routes_sandbox, routes_settings, routes_skills

    router = APIRouter()
    for module in (routes_archetypes, routes_cases, routes_sandbox, routes_skills, routes_settings):
        router.include_router(module.router)
    return router
