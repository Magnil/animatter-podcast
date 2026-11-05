"""FastAPI application factory.

Security posture (AGENT.md §11): loopback-only binding, no auth (single local
user), and an Origin check on state-changing requests so a random page in the
same browser cannot drive the local API.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from animatter.config import Settings, load_settings
from animatter.db import Database
from animatter.errors import install_error_handlers
from animatter.log import configure_logging, get_logger
from animatter.migrate import upgrade_to_head
from animatter.routers import health, hotwords, projects, transcribe

VERSION = "0.1.0"

_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_LOCAL_HOSTNAMES = frozenset({"127.0.0.1", "localhost", "[::1]", "::1"})


def _origin_is_local(origin: str) -> bool:
    if origin == "null":  # file:// pages, used by the packaged desktop shell
        return True
    try:
        parts = urlsplit(origin)
    except ValueError:
        return False
    hostname = parts.hostname or ""
    return parts.scheme in {"http", "https"} and hostname in {"127.0.0.1", "localhost", "::1"}


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    configure_logging(settings.log_level)
    log = get_logger("animatter.app")

    settings.ensure_dirs()
    upgrade_to_head(settings)

    app.state.db = Database(settings)
    log.info("app.ready", version=VERSION, data_dir=str(settings.data_dir), db=str(settings.db_path))
    try:
        yield
    finally:
        app.state.db.dispose()
        log.info("app.stopped")


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or load_settings()

    app = FastAPI(
        title="Animatter Podcast Studio API",
        version=VERSION,
        lifespan=lifespan,
    )
    app.state.settings = resolved
    app.state.version = VERSION

    install_error_handlers(app)

    @app.middleware("http")
    async def _origin_guard(request: Request, call_next):  # type: ignore[no-untyped-def]
        if request.method in _UNSAFE_METHODS:
            origin = request.headers.get("origin")
            if origin is not None and not _origin_is_local(origin):
                return JSONResponse(
                    status_code=403,
                    content={
                        "error": {
                            "code": "origin_rejected",
                            "message": f"拒绝来自 {origin} 的写请求",
                            "hint": "本地服务只接受同机页面发起的写操作；请从应用界面操作。",
                        }
                    },
                )
        started = time.perf_counter()
        response = await call_next(request)
        response.headers["x-elapsed-ms"] = f"{(time.perf_counter() - started) * 1000:.1f}"
        return response

    app.include_router(health.router)
    app.include_router(projects.router)
    app.include_router(transcribe.router)
    app.include_router(hotwords.router)
    return app


app = create_app()
