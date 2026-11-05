"""Skill graph endpoints: ``GET /skills/graph`` and ``POST /skills/reset``."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from fastapi.concurrency import run_in_threadpool

from axiom.api.deps import get_services
from axiom.api.schemas import SkillGraphEdgeView, SkillGraphNodeView, SkillGraphView
from axiom.models import OkResponse
from axiom.services import AppServices

__all__ = ["router", "build_graph_view"]

logger = logging.getLogger("axiom.api.skills")

router = APIRouter(prefix="/skills", tags=["skills"])


def build_graph_view(services: AppServices) -> SkillGraphView:
    """Graph response with section 12.5 ``summary``/``tier`` and both edge spellings."""
    with services.graph_lock:
        graph = services.graph
        base = graph.graph_response()
        pillars = graph.pillar_summary()
        nodes = [
            SkillGraphNodeView(**n.model_dump(), summary=graph.nodes[n.id].description, tier=graph.nodes[n.id].tier)
            for n in base.nodes
        ]
    edges = [SkillGraphEdgeView(src=e.src, dst=e.dst, source=e.src, target=e.dst) for e in base.edges]
    return SkillGraphView(nodes=nodes, edges=edges, pillars=pillars)


@router.get("/graph", response_model=SkillGraphView)
async def skill_graph(services: AppServices = Depends(get_services)) -> SkillGraphView:
    """All skill nodes with current (decayed) mastery, prerequisites and attempts."""
    return await run_in_threadpool(build_graph_view, services)


@router.post("/reset", response_model=OkResponse)
async def reset_skills(services: AppServices = Depends(get_services)) -> OkResponse:
    """Forget all mastery."""

    def _reset() -> None:
        with services.graph_lock:
            services.graph.reset()

    await run_in_threadpool(_reset)
    logger.info("skill mastery reset")
    return OkResponse(ok=True)
