"""FastAPI app assembly (sync — funread's storage layer is sync SQLAlchemy)."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware

from funread.legado.manage.source.storage import init_source_db
from funread.legado.reader import init_reader_db
from funread_api.accounts import init_auth_db, resolve_session_secret
from funread_api.security import SESSION_TTL, auth_enabled, resolve_cors_origins
from funread_api.v1 import api_router

logger = logging.getLogger("funread_api")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Create tables (if missing) on startup so config/connectivity errors
    # surface immediately instead of on the first request.
    init_source_db()
    init_reader_db()
    init_auth_db()
    if not auth_enabled():
        logger.warning(
            "管理端口令未配置：所有接口无鉴权开放。"
            "若本服务不是只监听 127.0.0.1，请立即在 config.toml 的 [auth] password "
            "里配置口令（或写进 funsecret 的 funread/api/auth/password）。"
        )
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="funread",
        version="0.1.0",
        description="Manage funread's source-list collection",
        lifespan=lifespan,
    )
    #  Reader sessions ride Starlette's signed cookie. The secret is persisted
    #  (see `resolve_session_secret`) rather than random-per-start: a month-long
    #  reading session must survive a restart, and this service gets restarted
    #  by whoever is self-hosting it far more often than a cloud deployment.
    app.add_middleware(
        SessionMiddleware,
        secret_key=resolve_session_secret(),
        max_age=SESSION_TTL,
        same_site="lax",
        #  Plain http on a LAN IP -- a secure cookie would never come back.
        https_only=False,
    )
    #  跨站来源。Web 前端是同源反代 `/api`，用不着 CORS；需要它的是**套壳和
    #  原生客户端** —— 它们把页面打包在 app 里，请求用户自己的服务器属于跨站。
    #
    #  `allow_credentials=False` 是故意的：跨站一律走 `Authorization: Bearer`，
    #  不让 cookie 参与。开了 credentials 既要求 origin 不能是通配，又会把会话
    #  cookie 暴露在跨站路径上，而我们本来就有设备 token 这条更合适的路。
    origins = resolve_cors_origins()
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=False,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    app.include_router(api_router, prefix="/api/v1")

    @app.get("/healthz", tags=["ops"])
    def healthz() -> dict:
        return {"status": "ok"}

    return app


app = create_app()
