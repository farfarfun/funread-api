"""FastAPI app assembly (sync — funread's storage layer is sync SQLAlchemy)."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from funread.legado.manage.source.storage import init_source_db
from funread.legado.reader import init_reader_db
from funread_api.security import auth_enabled
from funread_api.v1 import api_router

logger = logging.getLogger("funread_api")

#: Default bind address. Loopback, not 0.0.0.0: exposing the service to the
#: LAN is a deliberate act (reading from a phone), so it takes an explicit
#: FUNREAD_API_HOST -- and that is exactly when FUNREAD_API_PASSWORD matters.
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18811


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Create tables (if missing) on startup so config/connectivity errors
    # surface immediately instead of on the first request.
    init_source_db()
    init_reader_db()
    if not auth_enabled():
        logger.warning(
            "FUNREAD_API_PASSWORD 未配置：所有接口无鉴权开放。"
            "若本服务不是只监听 127.0.0.1，请立即配置口令。"
        )
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="funread",
        version="0.1.0",
        description="Manage funread's source-list collection",
        lifespan=lifespan,
    )
    app.include_router(api_router, prefix="/api/v1")

    @app.get("/healthz", tags=["ops"])
    def healthz() -> dict:
        return {"status": "ok"}

    return app


app = create_app()


def run() -> None:
    import uvicorn

    uvicorn.run(
        "funread_api.app:app",
        host=os.environ.get("FUNREAD_API_HOST", DEFAULT_HOST),
        port=int(os.environ.get("FUNREAD_API_PORT", DEFAULT_PORT)),
    )


if __name__ == "__main__":
    run()
