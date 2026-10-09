"""Two separate auth paths, deliberately not one.

**Admin (``/admin``, the collection console).** A pre-shared password plus an
hmac-signed cookie, no user record. ``POST /sources`` fetches an arbitrary URL
server-side (SSRF), so it sits behind this. No password configured means wide
open plus a startup WARN -- that keeps an existing localhost-only setup working
instead of breaking it on upgrade.

**Reader (``/web``, the reading front-end).** Real accounts in ``reader_user``,
scrypt password hashes, and a *different* cookie. The shelf, reading progress
and subscriptions are per-user, so every query is scoped by ``user_id``.

Keeping them apart is the point: logging in to read must not grant access to
the collection console, and the console's shared password must not identify a
reader. Both cookies are ``secure=False`` because this is reached over plain
http on a LAN -- which is also exactly why none of it should face the internet.

The reader session is signed with the user's *password hash*, so changing a
password invalidates that user's outstanding sessions. That is the only
revocation mechanism, and it is the one a self-hoster actually wants.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from dataclasses import dataclass

from fastapi import HTTPException, Request, status

from funread.legado.reader import LOCAL_USER_ID, count_users, get_user

#: Admin console cookie. Pre-shared password, no user record.
COOKIE_NAME = "funread_session"

#: Reader account cookie. Carries the user id.
USER_COOKIE_NAME = "funread_user"

#: Sessions last a month. This is a reading app opened from a phone -- being
#: logged out mid-book is a worse failure than a long-lived cookie on a LAN.
SESSION_TTL = 30 * 24 * 3600

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

#: Username reported for the implicit identity used while no account exists.
LOCAL_USERNAME = "local"


@dataclass(frozen=True)
class CurrentUser:
    """Who the request is acting as, on the reader side."""

    user_id: int
    username: str

    @property
    def is_local(self) -> bool:
        """True for the implicit accountless identity, not a real account."""
        return self.user_id == LOCAL_USER_ID


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


def registration_code() -> str | None:
    """``FUNREAD_REGISTER_CODE``, or ``None`` when registration is closed.

    Unset means ``POST /auth/register`` returns 403 outright. Defaulting to
    *closed* is the only safe default: anything that can reach the LAN address
    could otherwise open an account and spend the server's fetch budget.
    """
    value = os.environ.get("FUNREAD_REGISTER_CODE", "").strip()
    return value or None


def registration_open() -> bool:
    return registration_code() is not None


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
# Reader: real accounts
# ---------------------------------------------------------------------------


def _sign_user(password_hash: str, user_id: int, expires_at: int) -> str:
    return hmac.new(
        password_hash.encode("utf-8"), f"r1.{user_id}.{expires_at}".encode(), hashlib.sha256
    ).hexdigest()


def issue_user_token(user_id: int, password_hash: str, now: float | None = None) -> str:
    """Mint a reader session token: ``r1.<user_id>.<expires_at>.<hmac>``."""
    expires_at = int(now if now is not None else time.time()) + SESSION_TTL
    return f"r1.{user_id}.{expires_at}.{_sign_user(password_hash, user_id, expires_at)}"


def resolve_user_token(
    token: str,
    *,
    database_url: str | None = None,
    now: float | None = None,
) -> CurrentUser | None:
    """Verify a reader token and return who it belongs to, or ``None``.

    Every failure mode -- malformed, expired, unknown user, disabled user, bad
    signature -- collapses to ``None``. The caller turns that into one 401, so
    a probe cannot tell "no such user" from "wrong signature".
    """
    parts = (token or "").split(".")
    if len(parts) != 4 or parts[0] != "r1":
        return None
    try:
        user_id = int(parts[1])
        expires_at = int(parts[2])
    except ValueError:
        return None
    if expires_at < (now if now is not None else time.time()):
        return None

    user = get_user(user_id, database_url=database_url)
    if user is None or user.disabled:
        return None
    if not hmac.compare_digest(parts[3], _sign_user(user.password_hash, user_id, expires_at)):
        return None
    return CurrentUser(user_id=user.user_id, username=user.username)


def current_user(request: Request, *, database_url: str | None = None) -> CurrentUser | None:
    """Who this request is acting as on the reader side, if anyone.

    The implicit local identity is granted only when **both** hold:

    - no account exists yet -- the moment someone registers, that fallback is
      gone and a cookie is required, or the first account's shelf would be
      readable by anyone on the LAN; and
    - the admin guard is satisfied, which with no password configured means
      "trivially". So a fresh clone and CI keep working untouched, while a
      setup that had set ``FUNREAD_API_PASSWORD`` to lock everything down does
      not silently lose that protection by upgrading into accounts.
    """
    resolved = resolve_user_token(
        request.cookies.get(USER_COOKIE_NAME, ""), database_url=database_url
    )
    if resolved is not None:
        return resolved
    if count_users(database_url=database_url) == 0 and is_authenticated(request):
        return LOCAL_USER
    return None


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


def _unauthorized() -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="需要登录")


def require_session(request: Request) -> None:
    """Admin console dependency: reject anything without a valid admin session."""
    if not is_authenticated(request):
        raise _unauthorized()


def require_reader(request: Request) -> None:
    """Read-only reader endpoints, honouring ``FUNREAD_READER_PUBLIC``.

    These are stateless: search, parse, fetch. They carry no user identity, so
    a public reader is a reasonable thing to want (share the LAN address, no
    login prompt). Anything that writes per-user state uses ``require_user``.
    """
    if is_authenticated(request) or current_user(request) is not None:
        return
    if request.method in _SAFE_METHODS and reader_is_public():
        return
    raise _unauthorized()


def require_user(request: Request) -> CurrentUser:
    """Per-user endpoints: shelf, progress, subscriptions.

    Never opened up by ``FUNREAD_READER_PUBLIC`` -- that flag is about
    *reading*, and someone else's shelf is not public reading material.
    """
    user = current_user(request)
    if user is None:
        raise _unauthorized()
    return user


__all__ = [
    "COOKIE_NAME",
    "LOCAL_USER",
    "LOCAL_USERNAME",
    "SESSION_TTL",
    "USER_COOKIE_NAME",
    "CurrentUser",
    "auth_enabled",
    "current_user",
    "is_authenticated",
    "issue_token",
    "issue_user_token",
    "reader_is_public",
    "registration_code",
    "registration_open",
    "require_reader",
    "require_session",
    "require_user",
    "resolve_api_password",
    "resolve_user_token",
    "verify_token",
]
