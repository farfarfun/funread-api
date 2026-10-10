"""Reader accounts (register / login) plus the admin console's shared password.

Two login endpoints on purpose: ``/auth/login`` identifies a *reader*,
``/auth/admin/login`` unlocks the *collection console*. They set different
cookies and grant different things -- see ``funread_api.security``.

The credential logic itself is `funauth`'s (bcrypt, invite codes, roles, and
the timing-equalised failure paths). These handlers are hand-written rather
than taken from ``funauth.contrib.fastapi.make_auth_router`` because this
service has two things that factory does not model: the admin console's shared
password living on the same ``/auth`` prefix, and the implicit local identity
that keeps a fresh clone working before any account exists. Response shapes
are therefore funread's own ``SessionState``, not funauth's ``UserOut``.
"""

from __future__ import annotations

import hmac

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from funauth import BadCredentials, InviteUnusable, UsernameTaken, UserRole
from pydantic import BaseModel, Field

from funread.legado.reader import claim_local_data
from funread_api.accounts import (
    MIN_PASSWORD_LENGTH,
    accounts,
    count_users,
    is_legacy_hash,
    registration_open,
    validate_credentials,
    verify_legacy_password,
)
from funread_api.security import (
    COOKIE_NAME,
    SESSION_TTL,
    CurrentUser,
    OptionalUser,
    SessionDep,
    auth_enabled,
    is_authenticated,
    issue_token,
    reader_is_public,
    resolve_api_password,
    session_store,
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
    #: 邀请码。空串照样送到服务端，这样失败是「邀请码不可用」而不是一个前端
    #: 要特殊处理的 422；首次运行引导那条路径本来就不需要它。
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
    #: funauth 角色（`admin` / `guest`）。未登录时 `None`。
    role: str | None = None


def _state(request: Request, user: CurrentUser | None) -> SessionState:
    return SessionState(
        auth_required=auth_enabled(),
        admin=is_authenticated(request),
        reader_public=reader_is_public(),
        register_open=registration_open(),
        authenticated=user is not None,
        user_id=user.user_id if user else None,
        username=user.username if user else None,
        local=bool(user and user.is_local),
        role=str(user.role) if user else None,
    )


def _login(request: Request, user: object) -> CurrentUser:
    """Write the session and project the funauth row onto ``CurrentUser``."""
    session_store.login(request, str(user.id))
    return CurrentUser(user_id=int(user.id), username=user.username, role=user.role)


@router.get("/me", response_model=SessionState)
async def me(request: Request, user: OptionalUser) -> SessionState:
    """Lets the front-end decide what to render: login, register, or the app."""
    return _state(request, user)


@router.post("/register", response_model=SessionState, status_code=status.HTTP_201_CREATED)
async def register(
    payload: RegisterRequest, request: Request, session: SessionDep
) -> SessionState:
    """Create a reader account.

    Two paths, and which one applies depends only on whether any account exists:

    **First-run bootstrap** (no account yet). No invite code needed, and the
    account is created as ``ADMIN``. This grants nothing that was not already
    on offer: with zero accounts, ``security.resolve_current_user`` already
    hands the implicit local identity to any request that satisfies the admin
    guard. So this path is gated on *exactly that same* condition -- otherwise
    a LAN-reachable instance with ``FUNREAD_API_PASSWORD`` set would have its
    lockdown bypassed by a registration. It is deliberately **exempt** from
    ``FUNREAD_REGISTER_OPEN``: that flag closes self-service signup by
    strangers, while this is the operator claiming their own box with the admin
    password. Honouring it here would make ``FUNREAD_REGISTER_OPEN=0`` a box
    that can never have a first account at all.

    **Invite code** (an account exists). Requires a code issued by
    ``funread-api accounts invite``, and produces a ``GUEST``. The four reasons
    a code can be unusable (unknown / revoked / expired / exhausted) share one
    message -- splitting them turns this into a "does this code exist" probe.
    """
    try:
        username = validate_credentials(payload.username, payload.password)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)
        ) from error

    is_first = await count_users(session) == 0
    if is_first:
        if not is_authenticated(request):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="首个账号需要先通过管理口令验证（POST /auth/admin/login）",
            )
        try:
            user = await accounts.create_user(session, username, payload.password, UserRole.ADMIN)
        except UsernameTaken as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=str(error)
            ) from error
    else:
        if not registration_open():
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="注册未开放：服务端设置了 FUNREAD_REGISTER_OPEN=0",
            )
        if not payload.code:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="需要邀请码")
        try:
            user = await accounts.register_with_invite(
                session, username, payload.password, payload.code
            )
        except InviteUnusable as error:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)
            ) from error
        except UsernameTaken as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=str(error)
            ) from error

    current = _login(request, user)
    if is_first:
        #  user 0 的书架 / 进度 / 订阅是这个人在还没有账号时读的，归他。
        #  claim_local_data 是同步的（阅读层整层同步），放线程池里跑，别阻塞事件循环。
        await run_in_threadpool(claim_local_data, current.user_id)
    return _state(request, current)


@router.post("/login", response_model=SessionState)
async def login(payload: LoginRequest, request: Request, session: SessionDep) -> SessionState:
    """Log in as a reader. One 401 for every failure -- see funauth's ``authenticate``."""
    existing = await accounts.get_by_username(session, (payload.username or "").strip())
    if existing is not None and is_legacy_hash(existing.password_hash):
        #  升级前就有的账号：哈希还是 scrypt，bcrypt 验不了。自己验一次，成功就
        #  当场换成 bcrypt —— 每个账号最多走这一次，用户不用重设口令。
        #  不交给 funauth.authenticate 的原因是它只认 bcrypt，会直接当成口令错。
        if not existing.is_active or not verify_legacy_password(
            payload.password, existing.password_hash
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或口令不正确"
            )
        await accounts.set_password(session, existing.username, payload.password)
        return _state(request, _login(request, existing))

    try:
        user = await accounts.authenticate(session, payload.username, payload.password)
    except BadCredentials as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=str(error)
        ) from error
    return _state(request, _login(request, user))


@router.post("/admin/login", response_model=SessionState)
async def admin_login(
    payload: AdminLoginRequest, request: Request, response: Response, user: OptionalUser
) -> SessionState:
    """Unlock the collection console. Shared password, not an account."""
    password = resolve_api_password()
    if password is None:
        # Nothing to log into. Say so rather than handing out a cookie that no
        # dependency would ever check.
        return _state(request, user)

    if not payload.password or not hmac.compare_digest(payload.password, password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="口令不正确")

    response.set_cookie(
        key=COOKIE_NAME,
        value=issue_token(password),
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
    #  `is_authenticated` reads the *request* cookie, which this response has
    #  not reached yet -- so patch the two fields the caller just earned
    #  instead of reporting the pre-login state back.
    state = _state(request, user)
    state.auth_required = True
    state.admin = True
    return state


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def logout(request: Request, response: Response) -> None:
    """Drop both identities. Logging out of one and not the other is never wanted."""
    session_store.logout(request)
    response.delete_cookie(key=COOKIE_NAME, path="/")


class AccountSummary(BaseModel):
    #: How many accounts exist. The front-end uses 0 to mean "first-run".
    users: int
    register_open: bool
    min_password_length: int
    #: True while the first-run path is available (no account yet), i.e. the
    #: registration form must not ask for an invite code.
    bootstrap: bool


@router.get("/accounts", response_model=AccountSummary)
async def account_summary(session: SessionDep) -> AccountSummary:
    """First-run probe. Deliberately exposes only a count, never a user list."""
    users = await count_users(session)
    return AccountSummary(
        users=users,
        register_open=registration_open(),
        min_password_length=MIN_PASSWORD_LENGTH,
        bootstrap=users == 0,
    )
