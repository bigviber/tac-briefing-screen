"""LLM settings, connection test, usage and prompt inspector endpoints.

The API key is stored only in the app DB. ``GET`` returns it masked; ``PUT`` keeps the stored
key when the body's ``api_key`` is empty or is the masked value echoed back.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from fastapi.concurrency import run_in_threadpool

from axiom.api.deps import get_services
from axiom.api.schemas import LLMSettingsView
from axiom.engine.pedagogy import list_prompts
from axiom.models import LLMSettings, LLMTestResult, LLMUsageSummary, PromptTemplate, mask_api_key
from axiom.services import LLM_NOT_CONFIGURED, AppServices

__all__ = ["router", "merge_api_key"]

logger = logging.getLogger("axiom.api.settings")

router = APIRouter(prefix="/settings", tags=["settings"])


def merge_api_key(incoming: LLMSettings, stored: LLMSettings | None) -> LLMSettings:
    """Keep the stored key when ``incoming.api_key`` is blank or a masked echo of it."""
    key = (incoming.api_key or "").strip()
    if stored is not None and stored.api_key:
        masked = mask_api_key(stored.api_key)
        if not key or key == masked or key.startswith("****") or "…" in key:
            key = stored.api_key
    return incoming.model_copy(update={"api_key": key})


@router.get("/llm", response_model=LLMSettingsView)
async def get_llm_settings(services: AppServices = Depends(get_services)) -> LLMSettingsView:
    """Current LLM settings with the API key masked."""
    return LLMSettingsView.build(await run_in_threadpool(services.llm_settings))


@router.put("/llm", response_model=LLMSettingsView)
async def put_llm_settings(body: LLMSettings, services: AppServices = Depends(get_services)) -> LLMSettingsView:
    """Store LLM settings (applies to the next request; no restart needed)."""

    def _put() -> LLMSettings:
        merged = merge_api_key(body, services.persistence.get_llm_settings())
        services.persistence.put_llm_settings(merged)
        return merged

    return LLMSettingsView.build(await run_in_threadpool(_put))


@router.delete("/llm", response_model=LLMSettingsView)
async def clear_llm_settings(services: AppServices = Depends(get_services)) -> LLMSettingsView:
    """Forget the stored API key and models; the next request sees an unconfigured LLM."""

    def _clear() -> LLMSettings:
        services.persistence.clear_llm_settings()
        return services.llm_settings()

    return LLMSettingsView.build(await run_in_threadpool(_clear))


@router.post("/llm/test", response_model=LLMTestResult)
async def test_llm(services: AppServices = Depends(get_services)) -> LLMTestResult:
    """One tiny completion against the configured fast model."""
    settings = await run_in_threadpool(services.llm_settings)
    if not settings.configured:
        return LLMTestResult(ok=False, model=settings.fast_model, latency_ms=0.0, error=LLM_NOT_CONFIGURED)

    def _test() -> LLMTestResult:
        client = services.llm_client(settings)
        try:
            return client.test_connection()
        finally:
            client.close()

    result = await run_in_threadpool(_test)
    if result.error:
        from axiom.api.errors import scrub

        result.error = scrub(result.error)
    return result


@router.get("/usage", response_model=LLMUsageSummary)
async def usage(services: AppServices = Depends(get_services)) -> LLMUsageSummary:
    """Token usage and estimated cost by model."""
    return await run_in_threadpool(services.persistence.usage_summary)


@router.get("/prompts", response_model=list[PromptTemplate])
async def prompts() -> list[PromptTemplate]:
    """Every prompt template as the model sees it (read-only inspector)."""
    return list_prompts()
