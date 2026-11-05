"""Case endpoints: generate, list, get, schema, dossier, textbook, session, submit, adversary,
advance (ARCHITECTURE.md sections 5, 11 and 12).

Every blocking call (world generation, file IO, LLM calls through :class:`Pedagogy`) runs in
the threadpool; the LLM endpoints accept ``?stream=1`` (see :mod:`axiom.api.streaming`).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.concurrency import run_in_threadpool

from axiom.api.deps import get_services
from axiom.api.errors import CaseStateError
from axiom.api.schemas import CasePublicView, GradeReportPublic, SessionState, ThreadMessage, public_grade
from axiom.api.streaming import DeltaCallback, stream_or_call
from axiom.models import (
    AdvanceRequest,
    AdversaryRequest,
    AdversaryTurn,
    CaseSummary,
    DatabaseSpec,
    Dossier,
    DossierRequest,
    GenerateCaseRequest,
    SimQuarter,
    Submission,
    TextbookChapter,
    TextbookRequest,
)
from axiom.services import AppServices

__all__ = ["router", "build_session"]

logger = logging.getLogger("axiom.api.cases")

router = APIRouter(prefix="/cases", tags=["cases"])

_STREAM_CHUNK = 24


# --------------------------------------------------------------------------- generate / read


@router.post("/generate", response_model=CasePublicView)
async def generate_case(body: GenerateCaseRequest | None = None, services: AppServices = Depends(get_services)) -> CasePublicView:
    """Generate a new case; without ``archetype`` the skill graph picks one."""
    return await run_in_threadpool(services.world_service.generate, body or GenerateCaseRequest())


@router.get("", response_model=list[CaseSummary])
async def list_cases(services: AppServices = Depends(get_services)) -> list[CaseSummary]:
    """All cases, newest first."""
    return await run_in_threadpool(services.persistence.list_cases)


@router.get("/{case_id}", response_model=CasePublicView)
async def get_case(case_id: str, services: AppServices = Depends(get_services)) -> CasePublicView:
    """Public view of a case (no ground truth) with asks, levers and status."""

    def _get() -> CasePublicView:
        return services.world_service.public(services.world_service.load(case_id))

    return await run_in_threadpool(_get)


@router.get("/{case_id}/schema", response_model=DatabaseSpec)
async def get_schema(case_id: str, services: AppServices = Depends(get_services)) -> DatabaseSpec:
    """The case database spec (tables, columns, dirt, sample rows, ERD)."""
    world = await run_in_threadpool(services.world_service.load, case_id)
    return world.database


# --------------------------------------------------------------------------- LLM artifacts


@router.post("/{case_id}/dossier", response_model=Dossier)
async def dossier(
    case_id: str,
    body: DossierRequest | None = None,
    stream: bool = Query(False, description="1 = text/event-stream"),
    services: AppServices = Depends(get_services),
) -> Any:
    """The case dossier (LLM; cached unless ``force``)."""
    world = await run_in_threadpool(services.world_service.load, case_id)
    force = bool(body.force) if body is not None else False

    def work(on_delta: DeltaCallback | None) -> Dossier:
        with services.pedagogy() as ped:
            return ped.synthesize_dossier(world, stream=on_delta is not None, force=force, on_delta=on_delta)

    return await stream_or_call(work, stream)


@router.post("/{case_id}/textbook", response_model=TextbookChapter)
async def textbook(
    case_id: str,
    body: TextbookRequest,
    stream: bool = Query(False, description="1 = text/event-stream"),
    services: AppServices = Depends(get_services),
) -> Any:
    """A textbook chapter for one skill node (LLM; cached). 409 while locked (principal)."""
    world = await run_in_threadpool(services.world_service.load, case_id)

    def work(on_delta: DeltaCallback | None) -> TextbookChapter:
        with services.pedagogy() as ped:
            return ped.textbook_chapter(world, body.skill_node, stream=on_delta is not None, on_delta=on_delta)

    return await stream_or_call(work, stream)


# --------------------------------------------------------------------------- session


def build_session(services: AppServices, case_id: str) -> SessionState:
    """Latest submission, its grade, the adversary thread and the simulation history."""
    services.world_service.record(case_id)
    db = services.persistence
    subs = db.list_submissions(case_id)
    graded = [s for s in subs if s.grade]
    latest = graded[-1] if graded else (subs[-1] if subs else None)
    state = SessionState(status=services.world_service.status(case_id))
    if latest is None:
        state.sim_history = db.list_sim_quarters(case_id)
        return state
    state.submission_id = latest.submission_id
    state.grade = public_grade(latest.grade) if latest.grade else None
    for turn in db.list_turns(latest.submission_id):
        payload = turn.payload
        if turn.role == "adversary":
            adv = AdversaryTurn.model_validate({**payload, "turn_index": turn.turn_index})
            state.adversary_turns.append(adv)
            state.thread.append(ThreadMessage(role="adversary", message=adv.message, turn_index=turn.turn_index,
                                              persona=adv.persona, created_at=turn.created_at))
        else:
            state.thread.append(ThreadMessage(role=turn.role, message=str(payload.get("message", "")),
                                              turn_index=turn.turn_index, created_at=turn.created_at))
    state.sim_history = db.list_sim_quarters(case_id, latest.submission_id)
    return state


@router.get("/{case_id}/session", response_model=SessionState)
async def session(case_id: str, services: AppServices = Depends(get_services)) -> SessionState:
    """Everything the workbench needs to restore after a reload (section 12.3)."""
    return await run_in_threadpool(build_session, services, case_id)


# --------------------------------------------------------------------------- grading loop


@router.post("/{case_id}/submit", response_model=GradeReportPublic)
async def submit(case_id: str, body: Submission, services: AppServices = Depends(get_services)) -> GradeReportPublic:
    """Grade a submission: deterministic check (reference SQL executed read-only) + LLM rubric."""
    submission = body.model_copy(update={"case_id": case_id})

    def _grade() -> GradeReportPublic:
        ws = services.world_service
        world = ws.load(case_id)
        previous = ws.status(case_id)
        services.llm_client().close()  # fail fast (428) before touching the case status
        runner = None
        db_path = ws.db_path(case_id)
        if db_path.is_file():
            runner = services.sql_sandbox(case_id).run_reference
        with services.case_lock(case_id):
            services.persistence.set_case_status(case_id, "submitted")
            try:
                with services.pedagogy() as ped, services.graph_lock:
                    report = ped.grade(world, submission, sql_runner=runner)
            except BaseException:
                services.persistence.set_case_status(case_id, previous)
                raise
        logger.info("graded %s: overall=%.1f accuracy=%.2f", report.submission_id, report.rubric.overall,
                    report.deterministic.accuracy)
        return public_grade(report)

    return await run_in_threadpool(_grade)


@router.post("/{case_id}/adversary", response_model=AdversaryTurn)
async def adversary(
    case_id: str,
    body: AdversaryRequest,
    stream: bool = Query(False, description="1 = text/event-stream"),
    services: AppServices = Depends(get_services),
) -> Any:
    """Next Socratic adversary turn. With ``?stream=1`` the message is streamed in chunks."""
    world = await run_in_threadpool(services.world_service.load, case_id)

    def work(on_delta: DeltaCallback | None) -> AdversaryTurn:
        with services.pedagogy() as ped:
            try:
                turn = ped.adversary_turn(world, body.submission_id, body.message)
            except ValueError as exc:
                raise CaseStateError(str(exc)) from exc
        if on_delta is not None:
            for i in range(0, len(turn.message), _STREAM_CHUNK):
                on_delta(turn.message[i:i + _STREAM_CHUNK])
        return turn

    return await stream_or_call(work, stream)


@router.post("/{case_id}/advance", response_model=SimQuarter)
async def advance(case_id: str, body: AdvanceRequest, services: AppServices = Depends(get_services)) -> SimQuarter:
    """Simulate the next quarter from lever decisions; ``world.json`` gets the new simulation."""

    def _advance() -> SimQuarter:
        ws = services.world_service
        ws.record(case_id)
        with services.case_lock(case_id):
            world = ws.load(case_id)
            with services.pedagogy() as ped:
                result = ped.advance_quarter(world, body.submission_id, body.decisions)
            ws.save_simulation(case_id, result.simulation)
        return result.quarter

    return await run_in_threadpool(_advance)
