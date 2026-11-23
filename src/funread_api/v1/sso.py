"""Casdoor OIDC login for reader accounts."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from urllib.parse import urlsplit

import sqlalchemy as sa
from authlib.integrations.base_client.errors import OAuthError
from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from funauth import UserRole
from joserfc.errors import JoseError
from sqlalchemy.exc import IntegrityError
from starlette.applications import Starlette

from funread.base import config
from funread_api.accounts import ReaderUser
from funread_api.security import SessionDep, session_store

router = APIRouter(prefix="/auth/oidc", tags=["auth"])


@dataclass(frozen=True)
class OidcConfig:
    issuer: str
    client_id: str
    client_secret: str
    redirect_uri: str
    admin_subject: str
    timeout_seconds: float


def load_config() -> OidcConfig | None:
    keys = ("issuer", "client_id", "client_secret", "redirect_uri", "admin_subject")
    values = {
        key: str(config.resolve_setting(f"oidc_{key}", candidates=(("oidc", key),)) or "").strip()
        for key in keys
    }
    if not any(values.values()):
        return None
    if not all(values.values()):
        raise RuntimeError(
            "[oidc] issuer、client_id、client_secret、redirect_uri、admin_subject 必须全部配置"
        )
    issuer = values["issuer"].rstrip("/")
    parsed_issuer = urlsplit(issuer)
    parsed_redirect = urlsplit(values["redirect_uri"])
    if (
        parsed_issuer.scheme != "https"
        or not parsed_issuer.hostname
        or parsed_issuer.path
        or parsed_issuer.query
        or parsed_issuer.fragment
        or len(issuer) > 255
    ):
        raise RuntimeError("[oidc] issuer 必须是 HTTPS 域名")
    if (
        parsed_redirect.scheme != "https"
        or not parsed_redirect.hostname
        or parsed_redirect.query
        or parsed_redirect.fragment
    ):
        raise RuntimeError("[oidc] redirect_uri 必须是无查询参数的 HTTPS 地址")
    raw_timeout = config.resolve_setting(
        "oidc_timeout_seconds", candidates=(("oidc", "timeout_seconds"),), fallback=120
    )
    try:
        timeout = float(raw_timeout)
    except (TypeError, ValueError) as error:
        raise RuntimeError("[oidc] timeout_seconds 必须是正数") from error
    if not math.isfinite(timeout) or timeout <= 0:
        raise RuntimeError("[oidc] timeout_seconds 必须是正数")
    return OidcConfig(
        issuer=issuer,
        client_id=values["client_id"],
        client_secret=values["client_secret"],
        redirect_uri=values["redirect_uri"],
        admin_subject=values["admin_subject"],
        timeout_seconds=timeout,
    )


def configure(app: Starlette) -> bool:
    oidc = load_config()
    app.state.oidc_config = oidc
    if oidc is None:
        return False
    oauth = OAuth()
    app.state.oidc_client = oauth.register(
        name="casdoor",
        client_id=oidc.client_id,
        client_secret=oidc.client_secret,
        server_metadata_url=f"{oidc.issuer}/.well-known/openid-configuration",
        client_kwargs={
            "scope": "openid profile email",
            "code_challenge_method": "S256",
            "timeout": oidc.timeout_seconds,
        },
    )
    return True


def _next_path(value: str) -> str:
    if value in ("/web", "/admin") or value.startswith(("/web/", "/admin/")):
        if not value.startswith("//") and "\\" not in value:
            return value
    return "/web"


def _username(issuer: str, subject: str, preferred: str) -> str:
    digest = hashlib.sha256(f"{issuer}\0{subject}".encode()).hexdigest()[:16]
    base = "".join(c if c.isalnum() or c in "_-" else "_" for c in preferred).strip("_")[:11]
    return f"sso_{base or 'user'}_{digest}"


@router.get("/config")
async def oidc_config(request: Request) -> dict[str, bool]:
    return {"enabled": request.app.state.oidc_config is not None}


@router.get("/login")
async def oidc_login(request: Request, next: str = "/web") -> RedirectResponse:
    oidc: OidcConfig | None = request.app.state.oidc_config
    if oidc is None:
        raise HTTPException(404, "未配置单点登录")
    request.session["sso_next"] = _next_path(next)
    return await request.app.state.oidc_client.authorize_redirect(request, oidc.redirect_uri)


@router.get("/callback")
async def oidc_callback(request: Request, session: SessionDep) -> RedirectResponse:
    oidc: OidcConfig | None = request.app.state.oidc_config
    if oidc is None:
        raise HTTPException(404, "未配置单点登录")
    try:
        token = await request.app.state.oidc_client.authorize_access_token(request)
    except (OAuthError, JoseError) as error:
        raise HTTPException(400, "单点登录校验失败") from error
    claims = token.get("userinfo")
    if (
        not token.get("id_token")
        or not claims
        or claims.get("iss") != oidc.issuer
        or not claims.get("sub")
    ):
        raise HTTPException(400, "单点登录身份无效")
    subject = str(claims["sub"])
    if len(subject) > 255:
        raise HTTPException(400, "单点登录身份过长")

    query = sa.select(ReaderUser).where(
        ReaderUser.oidc_issuer == oidc.issuer, ReaderUser.oidc_subject == subject
    )
    user = await session.scalar(query)
    role = UserRole.ADMIN if subject == oidc.admin_subject else UserRole.GUEST
    if user is None:
        preferred = str(claims.get("preferred_username") or subject.rsplit("/", 1)[-1])
        user = ReaderUser(
            username=_username(oidc.issuer, subject, preferred),
            password_hash=None,
            role=role,
            oidc_issuer=oidc.issuer,
            oidc_subject=subject,
        )
        session.add(user)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            user = await session.scalar(query)
            if user is None:
                raise
    if not user.is_active:
        raise HTTPException(403, "账号已停用")
    if user.role != role:
        user.role = role
        await session.commit()
    session_store.login(request, str(user.id))
    return RedirectResponse(_next_path(request.session.pop("sso_next", "/web")), status_code=303)
