"""Reader accounts (register / login) plus the admin console's shared password.

Two login endpoints on purpose: ``/auth/login`` identifies a *reader*,
``/auth/admin/login`` unlocks the *collection console*. They set different
cookies and grant different things -- see ``funread_api.security``.
"""

from __future__ import annotations

import hmac

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from funread.legado.reader import (
    MIN_PASSWORD_LENGTH,
    authenticate,
    count_users,
    create_user,
)
from funread_api.security import (
    COOKIE_NAME,
    SESSION_TTL,
    USER_COOKIE_NAME,
    auth_enabled,
    current_user,
    is_authenticated,
    issue_token,
    issue_user_token,
    reader_is_public,
    registration_code,
    registration_open,
    resolve_api_password,
)

router = APIRouter(prefix="/auth", tags=["auth"])


class AdminLoginRequest(BaseModel):
    password: str


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=1, max_length=256)


class RegisterRequest(BaseModel):
    username: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=1, max_length=256)
    #: The invite code. Empty is still sent through so the failure is "wrong
    #: code" rather than a validation error the UI has to special-case.
    code: str = ""


class SessionState(BaseModel):
    #: Whether the *admin console* needs a password at all.
    auth_required: bool
    #: Whether this request holds a valid admin session.
    admin: bool
    #: Whether read-only reader GETs are open (``FUNREAD_READER_PUBLIC``).
    reader_public: bool
    #: Whether ``POST /auth/register`` would be accepted at all.
    register_open: bool
    #: Whether this request is acting as some reader identity.
    authenticated: bool
    user_id: int | None
    username: str | None
    #: True while no account exists and the implicit local identity is in use.
    local: bool


def _state(request: Request) -> SessionState:
    user = current_user(request)
    return SessionState(
        auth_required=auth_enabled(),
        admin=is_authenticated(request),
        reader_public=reader_is_public(),
        register_open=registration_open(),
        authenticated=user is not None,
        user_id=user.user_id if user else None,
        username=user.username if user else None,
        local=bool(user and user.is_local),
    )


def _set_user_cookie(response: Response, user_id: int, password_hash: str) -> None:
    response.set_cookie(
        key=USER_COOKIE_NAME,
        value=issue_user_token(user_id, password_hash),
        max_age=SESSION_TTL,
        httponly=True,
        samesite="lax",
        path="/",
        #  Deliberately not secure=True: this is self-hosted and normally
        #  reached over plain http on a LAN IP, where a secure cookie would
        #  simply never be sent back. It is also why this must not face the
        #  internet -- see docs/project/project-overview.md 「范围外」.
        secure=False,
    )


@router.get("/me", response_model=SessionState)
def me(request: Request) -> SessionState:
    """Lets the front-end decide what to render: login, register, or the app."""
    return _state(request)


@router.post("/register", response_model=SessionState, status_code=status.HTTP_201_CREATED)
def register(payload: RegisterRequest, request: Request, response: Response) -> SessionState:
    """Create a reader account. Gated by ``FUNREAD_REGISTER_CODE``.

    Closed by default: with the code unset this is a flat 403, because anything
    that can reach the LAN address could otherwise open an account.
    """
    expected = registration_code()
    if expected is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="注册未开放：服务端未配置 FUNREAD_REGISTER_CODE",
        )
    #  compare_digest: a plain == would leak the code by timing
    if not hmac.compare_digest(payload.code or "", expected):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="邀请码不正确")

    try:
        user = create_user(payload.username, payload.password)
    except ValueError as error:
        #  Username taken is a conflict; everything else is the caller's input.
        code = status.HTTP_409_CONFLICT if "已被占用" in str(error) else status.HTTP_400_BAD_REQUEST
        raise HTTPException(status_code=code, detail=str(error)) from error

    _set_user_cookie(response, user.user_id, user.password_hash)
    return SessionState(
        auth_required=auth_enabled(),
        admin=is_authenticated(request),
        reader_public=reader_is_public(),
        register_open=True,
        authenticated=True,
        user_id=user.user_id,
        username=user.username,
        local=False,
    )


@router.post("/login", response_model=SessionState)
def login(payload: LoginRequest, request: Request, response: Response) -> SessionState:
    """Log in as a reader. One 401 for every failure -- see ``authenticate``."""
    user = authenticate(payload.username, payload.password)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或口令不正确")
    _set_user_cookie(response, user.user_id, user.password_hash)
    return SessionState(
        auth_required=auth_enabled(),
        admin=is_authenticated(request),
        reader_public=reader_is_public(),
        register_open=registration_open(),
        authenticated=True,
        user_id=user.user_id,
        username=user.username,
        local=False,
    )


@router.post("/admin/login", response_model=SessionState)
def admin_login(payload: AdminLoginRequest, request: Request, response: Response) -> SessionState:
    """Unlock the collection console. Shared password, not an account."""
    password = resolve_api_password()
    if password is None:
        # Nothing to log into. Say so rather than handing out a cookie that no
        # dependency would ever check.
        return _state(request)

    if not payload.password or not hmac.compare_digest(payload.password, password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="口令不正确")

    response.set_cookie(
        key=COOKIE_NAME,
        value=issue_token(password),
        max_age=SESSION_TTL,
        httponly=True,
        samesite="lax",
        path="/",
        secure=False,
    )
    user = current_user(request)
    return SessionState(
        auth_required=True,
        admin=True,
        reader_public=reader_is_public(),
        register_open=registration_open(),
        authenticated=user is not None,
        user_id=user.user_id if user else None,
        username=user.username if user else None,
        local=bool(user and user.is_local),
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
def logout(response: Response) -> None:
    """Drop both cookies. Logging out of one and not the other is never wanted."""
    response.delete_cookie(key=COOKIE_NAME, path="/")
    response.delete_cookie(key=USER_COOKIE_NAME, path="/")


class AccountSummary(BaseModel):
    #: How many accounts exist. The front-end uses 0 to mean "first-run".
    users: int
    register_open: bool
    min_password_length: int


@router.get("/accounts", response_model=AccountSummary)
def accounts() -> AccountSummary:
    """First-run probe. Deliberately exposes only a count, never a user list."""
    return AccountSummary(
        users=count_users(),
        register_open=registration_open(),
        min_password_length=MIN_PASSWORD_LENGTH,
    )
