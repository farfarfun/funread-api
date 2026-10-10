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
import os
import time
from dataclasses import dataclass
from typing import Annotated, Optional

from fastapi import Depends, HTTPException, Request, status
from funauth.contrib.fastapi import CookieSessionStore
from sqlalchemy.ext.asyncio import AsyncSession

from funread.legado.reader import LOCAL_USER_ID
from funread_api.accounts import UserRole, accounts, count_users, get_session

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


LOCAL_USER = CurrentUser(user_id=LOCAL_USER_ID, username=LOCAL_USERNAME)


# ---------------------------------------------------------------------------
# Admin: pre-shared password
# ---------------------------------------------------------------------------


def _read_secret() -> str | None:
    try:
        from funsecret import read_secret

        value = read_secret(cate1="funread", cate2="api", cate3="auth", cate4="password")
    except Exception:
        # No secret store configured is the normal case for a fresh clone or CI.
        return None
    return str(value) if value else None


def resolve_api_password() -> str | None:
    """Env ``FUNREAD_API_PASSWORD`` > funsecret ``funread/api/auth/password`` > None.

    Resolved per call rather than cached: tests monkeypatch the env, and a
    cached ``None`` from import time would make auth untestable and would also
    mean a password added after startup never takes effect.
    """
    env_value = os.environ.get("FUNREAD_API_PASSWORD")
    if env_value:
        return env_value
    return _read_secret()


def auth_enabled() -> bool:
    return resolve_api_password() is not None


def reader_is_public() -> bool:
    """Whether read-only reader GETs bypass auth (``FUNREAD_READER_PUBLIC=1``)."""
    return os.environ.get("FUNREAD_READER_PUBLIC", "").strip().lower() in {"1", "true", "yes"}


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
      setup that had set ``FUNREAD_API_PASSWORD`` to lock everything down does
      not silently lose that protection by upgrading into accounts.
    """
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
    """Read-only reader endpoints, honouring ``FUNREAD_READER_PUBLIC``.

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

    Never opened up by ``FUNREAD_READER_PUBLIC`` -- that flag is about
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
    "resolve_current_user",
    "session_store",
    "verify_token",
]
