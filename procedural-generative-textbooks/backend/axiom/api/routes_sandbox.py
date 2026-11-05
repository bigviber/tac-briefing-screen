"""Sandbox endpoints: ``POST /sandbox/sql`` and ``POST /sandbox/python``."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.concurrency import run_in_threadpool

from axiom.api.deps import get_services
from axiom.api.schemas import SqlQueryResultView
from axiom.models import PythonRunRequest, PythonRunResult, SqlQueryRequest
from axiom.services import AppServices

__all__ = ["router", "MAX_SQL_ROWS"]

router = APIRouter(prefix="/sandbox", tags=["sandbox"])

MAX_SQL_ROWS = 10_000


@router.post("/sql", response_model=SqlQueryResultView)
async def run_sql(body: SqlQueryRequest, services: AppServices = Depends(get_services)) -> SqlQueryResultView:
    """Run one read-only statement against the case DB (400 if the allowlist rejects it)."""
    sandbox = await run_in_threadpool(services.sql_sandbox, body.case_id)
    limit = min(int(body.limit), MAX_SQL_ROWS)
    result = await run_in_threadpool(sandbox.run, body.sql, limit)
    return SqlQueryResultView.model_validate(result)


@router.post("/python", response_model=PythonRunResult)
async def run_python(body: PythonRunRequest, services: AppServices = Depends(get_services)) -> PythonRunResult:
    """Run Python against the case in a resource-limited subprocess."""
    sandbox = await run_in_threadpool(services.python_sandbox, body.case_id)
    ceiling = max(float(services.settings.sandbox_timeout_s), 60.0)
    timeout = min(float(body.timeout_s), ceiling)
    result = await run_in_threadpool(lambda: sandbox.run(body.code, timeout_s=timeout))
    return PythonRunResult.model_validate(result)
