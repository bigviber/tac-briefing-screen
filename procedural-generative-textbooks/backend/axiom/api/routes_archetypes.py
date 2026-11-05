"""``GET /archetypes`` (the archetype catalog, ``[]`` while Module 2 is absent) and ``GET /health``.

Note: ``main.create_app`` still registers its own inline ``/health`` (``HealthResponse``) before
this router is included, and Starlette serves the first matching route. Remove that inline
handler in ``main.py`` so this one (which adds ``archetypes_available``) is served.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool

from axiom import __version__
from axiom.api.schemas import HealthView
from axiom.models import ArchetypeInfo
from axiom.services import AppServices, load_archetype_catalog

__all__ = ["router", "health_view"]

router = APIRouter(tags=["archetypes"])


@router.get("/archetypes", response_model=list[ArchetypeInfo])
async def list_archetypes() -> list[ArchetypeInfo]:
    """Every registered archetype with its pillars, skill nodes and difficulty range."""
    return await run_in_threadpool(load_archetype_catalog)


def health_view(services: AppServices | None) -> HealthView:
    """Liveness, whether an LLM is configured, and how many archetypes are registered."""
    configured = bool(services is not None and services.llm_configured())
    return HealthView(status="ok", version=__version__, llm_configured=configured,
                      archetypes_available=len(load_archetype_catalog()))


@router.get("/health", response_model=HealthView, tags=["health"])
async def health(request: Request) -> HealthView:
    """``{status, version, llm_configured, archetypes_available}`` (works before services start)."""
    services: AppServices | None = getattr(request.app.state, "services", None)
    return await run_in_threadpool(health_view, services)
