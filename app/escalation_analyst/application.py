"""Subapplication factory: a single mount is the only change required in AI Box."""

from __future__ import annotations

from contextlib import asynccontextmanager
import logging
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException
from .settings import Settings
from .runtime import Runtime
from .errors import EscalationError
from .api import groups, clusters, workspace, reports, logistics, internal, performance, presets


def create_app(settings: Settings | None = None) -> FastAPI:
    runtime = Runtime(settings or Settings())

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        runtime.start_background()
        try:
            yield
        finally:
            runtime.close()

    app = FastAPI(title="Escalation Analyst", version="1.0.0", lifespan=lifespan)
    app.state.runtime = runtime

    @app.exception_handler(EscalationError)
    async def domain_error(_: Request, exc: EscalationError):
        return JSONResponse({"error": exc.message}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def invalid(_: Request, exc: RequestValidationError):
        return JSONResponse(
            {
                "error": "Invalid request",
                "fields": [
                    {"field": ".".join(map(str, e["loc"])), "message": e["msg"]} for e in exc.errors()[:20]
                ],
            },
            status_code=400,
        )

    @app.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException):
        return JSONResponse({"error": str(exc.detail)}, status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(Exception)
    async def unexpected(_: Request, exc: Exception):
        logging.getLogger("escalation_analyst").exception("Escalation request failed", exc_info=exc)
        return JSONResponse({"error": "Escalation service temporarily unavailable"}, status_code=500)

    for router in (
        groups.router,
        clusters.router,
        workspace.router,
        reports.router,
        logistics.router,
        internal.router,
        performance.router,
        presets.router,
    ):
        app.include_router(router)
    return app


def install(host: FastAPI) -> FastAPI:
    """Mount without changing host routes, error handlers, prompts or database storage."""
    module = create_app()
    host.mount("/api/escalation", module, name="escalation_analyst")
    # Mounted subapplications do not receive host lifespan events automatically.
    host_lifespan = host.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with host_lifespan(app) as state:
            async with module.router.lifespan_context(module):
                yield state

    host.router.lifespan_context = lifespan
    return module
