"""``GET /archetypes``: the archetype catalog (``[]`` while Module 2 is absent)."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool

from axiom.models import ArchetypeInfo
from axiom.services import load_archetype_catalog

__all__ = ["router"]

router = APIRouter(tags=["archetypes"])


@router.get("/archetypes", response_model=list[ArchetypeInfo])
async def list_archetypes() -> list[ArchetypeInfo]:
    """Every registered archetype with its pillars, skill nodes and difficulty range."""
    return await run_in_threadpool(load_archetype_catalog)
