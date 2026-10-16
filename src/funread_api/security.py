"""Two separate auth paths, deliberately not one.

**Admin (``/admin``, the collection console).** A pre-shared password plus an
hmac-signed cookie, no user record. ``POST /sources`` fetches an arbitrary URL
server-side (SSRF), so it sits behind this. No password configured means wide
open plus a startup WARN -- that keeps an existing localhost-only setup working
instead of breaking it on upgrade.

**Reader (``/web``, the reading front-end).** Real accounts in ``reader_user``,
run by `funauth` (bcrypt hashes, invite-code registration, two roles), and a
*different* cookie -- Starlette's signed session cookie, installed by
``app.py``. The shelf, reading progress and subscriptions are per-user, so
every query is scoped by ``user_id``.

Keeping them apart is the point: logging in to read must not grant access to
the collection console, and the console's shared password must not identify a
reader. Both cookies are ``secure=False`` because this is reached over plain
http on a LAN -- which is also exactly why none of it should face the internet.

Reader sessions carry **only the user id**, never the role: the role is read
back from the database on every request, so disabling an account takes effect
immediately instead of waiting out a month-long cookie.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass
from typing import Annotated, Optional

import sqlalchemy as sa
from fastapi import Depends, HTTPException, Request, status
from funauth.contrib.fastapi import CookieSessionStore
from sqlalchemy.ext.asyncio import AsyncSession

from funread.base import config
from funread.legado.reader import LOCAL_USER_ID
from funread.legado.reader.storage import utcnow
from funread_api.accounts import (
    ReaderDeviceToken,
    UserRole,
    accounts,
    count_users,
    get_session,
    hash_device_token,
)

#: Admin console cookie. Pre-shared password, no user record.
COOKIE_NAME = "funread_session"

#: Sessions last a month. This is a reading app opened from a phone -- being
#: logged out mid-book is a worse failure than a long-lived cookie on a LAN.
SESSION_TTL = 30 * 24 * 3600

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

#: Username reported for the implicit identity used while no account exists.
LOCAL_USERNAME = "local"

#: Where the reader login state lives. Shared with ``v1/auth.py`` so logging in
#: and reading the session back are the same mechanism.
session_store = CookieSessionStore()


@dataclass(frozen=True)
class CurrentUser:
    """Who the request is acting as, on the reader side."""

    user_id: int
    username: str
    role: UserRole = UserRole.GUEST

    @property
    def is_local(self) -> bool:
        """True for the implicit accountless identity, not a real account."""
        return self.user_id == LOCAL_USER_ID

    @property
    def is_admin(self) -> bool:
        return self.role == UserRole.ADMIN


#: 零账号时的隐式身份，**角色是 ADMIN**。它代表「这台机器的主人」：只有在一个
#: 账号都还没有、且管理端守卫已满足时才会发出去（见 `resolve_current_user`），
#: 而首注册也正是把第一个账号建成 ADMIN —— 同一个身份，不该因为还没落库就降级。
#:
#: 这个角色是管理端在新 clone 与 CI 上开箱即用的唯一依靠：管理端改按角色鉴权之后，
#: 留着默认的 GUEST 会让一个全新实例连自己的采集台都进不去。
LOCAL_USER = CurrentUser(user_id=LOCAL_USER_ID, username=LOCAL_USERNAME, role=UserRole.ADMIN)


#: 套壳客户端的固定来源。Capacitor 在 iOS 上是 `capacitor://localhost`，
#: Android 上是 `http://localhost`，旧版 Ionic 还会用 `ionic://localhost`。
#: 这三个是**客户端自己的** origin，不是用户服务器的地址，所以可以写死。
DEFAULT_CORS_ORIGINS = (
    "capacitor://localhost",
    "ionic://localhost",
    "http://localhost",
)


def resolve_cors_origins() -> list[str]:
    """允许跨站访问的来源：config.toml ``[auth] cors_origins`` > 套壳默认值。

    默认就带上套壳客户端的三个 origin —— 自托管的人不该为了装一个官方客户端
    还得先去改配置文件，而这三个来源是固定的、不是任意站点。

    写成空列表（``cors_origins = []``）可以彻底关掉 CORS 中间件：只用 Web 端
    的部署不需要它，少一层是一层。
    """
    configured = config.resolve_setting(
        "cors_origins",
        candidates=(("auth", "cors_origins"), (None, "cors_origins")),
    )
    if configured is None:
        return list(DEFAULT_CORS_ORIGINS)
    if isinstance(configured, str):
        #  config.toml 里写成逗号分隔的字符串也认 —— funsecret 那层只能给字符串
        return [item.strip() for item in configured.split(",") if item.strip()]
    return [str(item).strip() for item in configured if str(item).strip()]


# ---------------------------------------------------------------------------
# Admin: pre-shared password
# ---------------------------------------------------------------------------


def resolve_api_password() -> str | None:
    """管理端口令：config.toml ``[auth] password`` > funsecret > 不设（全开）。

    每次调用都重新解析，不缓存：import 期缓存下来的 ``None`` 会让鉴权没法测，也会
    让启动之后才加上的口令永远不生效。

    1.1.116 起密钥库**坏掉**会原样抛，而不是当成"没配口令"。以前那个 ``except
    Exception: return None`` 会把一个连不上的密钥库翻译成"全开"，方向正好反了。
    """
    value = config.resolve_setting(
        "api_password",
        candidates=(("auth", "password"), (None, "api_password")),
        secret=("api", "auth", "password"),
    )
    return str(value) if value else None


def auth_enabled() -> bool:
    return resolve_api_password() is not None


def reader_is_public() -> bool:
    """只读的阅读端 GET 是否免鉴权：config.toml ``[auth] reader_public``，默认关。"""
    return bool(
        config.resolve_setting(
            "reader_public",
            candidates=(("auth", "reader_public"), (None, "reader_public")),
            secret=("api", "auth", "reader_public"),
            fallback=False,
            coerce=config.as_bool,
        )
    )


def _sign(password: str, expires_at: int) -> str:
    return hmac.new(
        password.encode("utf-8"), f"v1.{expires_at}".encode(), hashlib.sha256
    ).hexdigest()


def issue_token(password: str, now: float | None = None) -> str:
    """Mint an admin session token: ``<expires_at>.<hmac>``.

    The password itself is the signing key, so changing the password
    invalidates every outstanding admin session.
    """
    expires_at = int(now if now is not None else time.time()) + SESSION_TTL
    return f"{expires_at}.{_sign(password, expires_at)}"


def verify_token(token: str, password: str, now: float | None = None) -> bool:
    expires_raw, _, signature = (token or "").partition(".")
    if not signature:
        return False
    try:
        expires_at = int(expires_raw)
    except ValueError:
        return False
    if expires_at < (now if now is not None else time.time()):
        return False
    #  compare_digest, not ==: a plain comparison leaks the signature by timing
    return hmac.compare_digest(signature, _sign(password, expires_at))


def is_authenticated(request: Request) -> bool:
    """Admin-side check: a valid admin cookie, or no password configured."""
    password = resolve_api_password()
    if password is None:
        return True
    return verify_token(request.cookies.get(COOKIE_NAME, ""), password)


# ---------------------------------------------------------------------------
# Reader: funauth accounts
# ---------------------------------------------------------------------------

SessionDep = Annotated[AsyncSession, Depends(get_session)]


def _bearer_token(request: Request) -> Optional[str]:
    """从 `Authorization: Bearer xxx` 里取 token。没有这个头返回 `None`。

    注意「头在但值为空」要当成**给了一个空 token**（返回空串）而不是没给 ——
    否则客户端把变量拼错成空串时会静默回落到 cookie，在本地测着是好的、装到
    手机上就 401。
    """
    header = request.headers.get("authorization") or ""
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return value.strip()


async def _user_from_device_token(session: AsyncSession, raw: str) -> Optional[CurrentUser]:
    """校验设备 token，顺带刷新 `last_seen_at`。"""
    if not raw:
        return None
    row = await session.scalar(
        sa.select(ReaderDeviceToken).where(ReaderDeviceToken.token_hash == hash_device_token(raw))
    )
    if row is None:
        return None
    user = await accounts.get_by_id(session, int(row.user_id))
    if user is None or not user.is_active:
        #  账号被停用时，它名下所有设备立刻失效 —— 不用逐个吊销 token
        return None
    #  `last_seen_at` 只是给人看「这台设备还在不在用」，不值得为它每个请求都写
    #  一次库。隔一小时以上才更新。
    now = utcnow()
    if row.last_seen_at is None or (now - row.last_seen_at).total_seconds() > 3600:
        row.last_seen_at = now
        await session.commit()
    return CurrentUser(user_id=int(user.id), username=user.username, role=user.role)


async def resolve_current_user(request: Request, session: AsyncSession) -> Optional[CurrentUser]:
    """Who this request is acting as on the reader side, if anyone.

    Every failure mode of a stale session -- id that isn't an int, deleted
    account, disabled account -- clears the session and collapses to ``None``,
    so the caller turns all of them into one 401 and a probe cannot tell them
    apart.

    The implicit local identity is granted only when **both** hold:

    - no account exists yet -- the moment someone registers, that fallback is
      gone and a login is required, or the first account's shelf would be
      readable by anyone on the LAN; and
    - the admin guard is satisfied, which with no password configured means
      "trivially". So a fresh clone and CI keep working untouched, while a
      setup that had set ``[auth] password`` to lock everything down does
      not silently lose that protection by upgrading into accounts.
    """
    #  Bearer 优先于 cookie：套壳 / 原生客户端把页面打包进 app 之后，对用户
    #  自己服务器的请求是跨站的，`SameSite=Lax` 的 cookie 发不出去。
    #
    #  给了 Bearer 就**不回落到 cookie**：两套凭据同时在场时静默用另一套，会让
    #  「token 过期了为什么还能用」这类问题完全无法排查。
    bearer = _bearer_token(request)
    if bearer is not None:
        return await _user_from_device_token(session, bearer)

    raw = session_store.current(request)
    if raw:
        try:
            user_id = int(raw)
        except (TypeError, ValueError):
            #  A cookie from the previous session scheme, or a rotated secret.
            #  "Please log in again" is the right answer, not a 500.
            session_store.logout(request)
            return None
        user = await accounts.get_by_id(session, user_id)
        if user is None or not user.is_active:
            session_store.logout(request)
            return None
        return CurrentUser(user_id=int(user.id), username=user.username, role=user.role)

    if await count_users(session) == 0 and is_authenticated(request):
        return LOCAL_USER
    return None


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


def _unauthorized() -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="需要登录")


async def get_current_user(request: Request, session: SessionDep) -> Optional[CurrentUser]:
    """The reader identity as a dependency, ``None`` when there is none.

    FastAPI caches dependencies per request, so the guards below share one
    resolution (and one database round trip) even when several of them apply
    to the same endpoint.
    """
    return await resolve_current_user(request, session)


#: ``None`` when nobody is logged in -- for endpoints that decide for themselves.
OptionalUser = Annotated[Optional[CurrentUser], Depends(get_current_user)]


def require_session(request: Request) -> None:
    """Admin console dependency: reject anything without a valid admin session."""
    if not is_authenticated(request):
        raise _unauthorized()


async def require_reader(request: Request, user: OptionalUser) -> None:
    """Read-only reader endpoints, honouring ``[auth] reader_public``.

    These are stateless: search, parse, fetch. They carry no user identity, so
    a public reader is a reasonable thing to want (share the LAN address, no
    login prompt). Anything that writes per-user state uses ``require_user``.
    """
    if is_authenticated(request) or user is not None:
        return
    if request.method in _SAFE_METHODS and reader_is_public():
        return
    raise _unauthorized()


async def require_user(user: OptionalUser) -> CurrentUser:
    """Per-user endpoints: shelf, progress, subscriptions.

    Never opened up by ``[auth] reader_public`` -- that flag is about
    *reading*, and someone else's shelf is not public reading material.
    """
    if user is None:
        raise _unauthorized()
    return user


async def require_reader_admin(user: OptionalUser) -> CurrentUser:
    """Reader-side admin: a logged-in account whose role is ``ADMIN``.

    Distinct from ``require_session``, which is the console's shared password.
    This one identifies *a person* -- used for issuing invite codes, where
    "who handed this out" matters.
    """
    if user is None:
        raise _unauthorized()
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员账号")
    return user


__all__ = [
    "COOKIE_NAME",
    "LOCAL_USER",
    "LOCAL_USERNAME",
    "SESSION_TTL",
    "CurrentUser",
    "OptionalUser",
    "SessionDep",
    "auth_enabled",
    "get_current_user",
    "is_authenticated",
    "issue_token",
    "reader_is_public",
    "require_reader",
    "require_reader_admin",
    "require_session",
    "require_user",
    "resolve_api_password",
    "resolve_cors_origins",
    "resolve_current_user",
    "session_store",
    "verify_token",
]
