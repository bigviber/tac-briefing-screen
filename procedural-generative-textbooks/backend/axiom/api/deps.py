"""FastAPI dependencies shared by the routers."""

from __future__ import annotations

from fastapi import HTTPException, Request

from axiom.services import AppServices

__all__ = ["get_services"]


def get_services(request: Request) -> AppServices:
    """The :class:`AppServices` built by the app lifespan."""
    services = getattr(request.app.state, "services", None)
    if services is None:
        raise HTTPException(status_code=503, detail="Axiom services are not initialised yet")
    return services
