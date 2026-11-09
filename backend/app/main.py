"""FastAPI application bootstrapping, middleware, error handling, lifespan."""

from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.v1 import router as v1_router
from app.config import Settings, get_settings

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    database = None
    try:
        from app.db.engine import Database
        database = Database(settings.db_path)
        app.state.db = database
        yield
    finally:
        if database is not None:
            database.close()
            app.state.db = None


def make_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI(
        title="My Tube Backend",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.state.settings = settings or get_settings()

    s = app.state.settings
    allowed: list[str] = ["http://localhost", "http://127.0.0.1"]
    if getattr(s, "allowed_hosts", None):
        allowed = [f"http://{h}" for h in s.allowed_hosts] + allowed
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(v1_router)

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        rid = request.headers.get("x-request-id") or str(uuid.uuid4())[:12]
        request.state.request_id = rid
        response = await call_next(request)
        response.headers["x-request-id"] = rid
        return response

    @app.exception_handler(Exception)
    async def unhandled_error(request: Request, exc: Exception):
        rid = getattr(request.state, "request_id", None)
        logger.exception("unhandled error: %s", exc)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "detail": "internal server error",
                "request_id": rid,
            },
        )

    return app


app = make_app()
