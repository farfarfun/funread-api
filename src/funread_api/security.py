"""Minimal pre-shared-password auth: hmac-signed session cookie, no user table.

Why there is any auth at all: the reader front-end is meant to be opened from a
phone, which means cross-device access, which means the service is reachable by
anything on the LAN. ``run()`` binds ``0.0.0.0`` and every write endpoint used
to be wide open -- including ``POST /sources``, which fetches an arbitrary URL
server-side (SSRF). That endpoint now sits behind ``require_session``.

Why it is this simple: single-user self-hosting. No tenants, no roles, no
refresh tokens. A pre-shared password plus a signed cookie is the whole model,
and it needs no new dependency -- ``hmac``/``hashlib`` are stdlib.

No password configured means wide open plus a startup WARN. That keeps the
existing localhost-only single-user setup working instead of breaking it on
upgrade.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time

from fastapi import HTTPException, Request, status

COOKIE_NAME = "funread_session"

#: Sessions last a month. This is a reading app opened from a phone -- being
#: logged out mid-book is a worse failure than a long-lived cookie on a LAN.
SESSION_TTL = 30 * 24 * 3600

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


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
    """Mint a session token: ``<expires_at>.<hmac>``.

    The password itself is the signing key, so changing the password
    invalidates every outstanding session. That is the only revocation
    mechanism there is, and it is the one a single user actually wants.
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
    password = resolve_api_password()
    if password is None:
        return True
    return verify_token(request.cookies.get(COOKIE_NAME, ""), password)


def _unauthorized() -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="需要登录")


def require_session(request: Request) -> None:
    """Router dependency: reject anything without a valid session."""
    if not is_authenticated(request):
        raise _unauthorized()


def require_reader(request: Request) -> None:
    """Like ``require_session``, but honours ``FUNREAD_READER_PUBLIC``.

    Only safe methods are let through: a public *reader* is a reasonable thing
    to want (share the LAN address, no login prompt); a public shelf writer is
    not, so POST/PUT/DELETE still need the cookie even when the flag is on.
    """
    if is_authenticated(request):
        return
    if request.method in _SAFE_METHODS and reader_is_public():
        return
    raise _unauthorized()


__all__ = [
    "COOKIE_NAME",
    "SESSION_TTL",
    "auth_enabled",
    "is_authenticated",
    "issue_token",
    "reader_is_public",
    "require_reader",
    "require_session",
    "resolve_api_password",
    "verify_token",
]
