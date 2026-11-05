"""Axiom backend entry point: ``create_app()`` and ``python main.py`` (uvicorn on 127.0.0.1).

The API is mounted at ``/api/v1``. When ``../frontend/dist`` exists it is served at ``/`` with
an SPA fallback (unknown non-API paths return ``index.html``).
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from axiom import __version__
from axiom.api import API_PREFIX, build_api_router
from axiom.api.errors import install_exception_handlers
from axiom.config import Settings, get_settings
from axiom.services import AppServices

__all__ = ["create_app", "FRONTEND_DIST"]

logger = logging.getLogger("axiom.main")

FRONTEND_DIST: Path = Path(__file__).resolve().parent.parent / "frontend" / "dist"


def _mount_frontend(app: FastAPI, dist: Path) -> None:
    """Serve the built SPA from ``dist`` with an ``index.html`` fallback for non-API paths."""
    root = dist.resolve()
    index = root / "index.html"

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str) -> FileResponse:
        if full_path == "api" or full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not Found")
        if full_path:
            candidate = (root / full_path).resolve()
            if candidate.is_file() and candidate.is_relative_to(root):
                return FileResponse(candidate)
        if not index.is_file():
            raise HTTPException(status_code=404, detail="frontend build has no index.html")
        return FileResponse(index, headers={"Cache-Control": "no-cache"})


def create_app(
    settings: Settings | None = None,
    *,
    services_factory: Callable[[], AppServices] | None = None,
    frontend_dist: Path | None = None,
) -> FastAPI:
    """Build the FastAPI app.

    Args:
        settings: runtime settings (default :func:`get_settings`); used for CORS and services.
        services_factory: builds the :class:`AppServices` in the lifespan (tests inject one on
            tmp dirs with a fake LLM transport).
        frontend_dist: built frontend folder (default ``../frontend/dist``; served only if present).
    """
    cfg = settings or get_settings()
    factory = services_factory or (lambda: AppServices(cfg))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        services = await run_in_threadpool(factory)
        app.state.services = services
        try:
            yield
        finally:
            app.state.services = None
            services.close()

    app = FastAPI(title="Axiom", version=__version__, lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(cfg.cors_origins),
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    install_exception_handlers(app)

    app.include_router(build_api_router(), prefix=API_PREFIX)

    dist = frontend_dist if frontend_dist is not None else FRONTEND_DIST
    if dist.is_dir():
        _mount_frontend(app, dist)
        logger.info("serving frontend from %s", dist)
    return app


if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(level=os.environ.get("AXIOM_LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    port = int(os.environ.get("AXIOM_PORT", "8000"))
    uvicorn.run(create_app(), host="127.0.0.1", port=port, reload=False)
