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

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from funauth import BadCredentials, InviteUnusable, LastAdmin, UsernameTaken, UserRole
from pydantic import BaseModel, Field

from funread.legado.reader import claim_local_data
from funread_api.accounts import (
    MIN_PASSWORD_LENGTH,
    ReaderDeviceToken,
    accounts,
    count_users,
    hash_device_token,
    is_legacy_hash,
    new_device_token,
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
    require_reader_admin,
    require_user,
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
    #: Whether read-only reader GETs are open (``[auth] reader_public``).
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
        #  「当前这个身份是不是管理员」，前端据此决定放不放行 /admin。
        #
        #  以前这里是 `is_authenticated(request)`，也就是「那个预共享口令验过了吗」
        #  —— 而口令没配时它恒为真，于是任何人打开页面都拿到 admin=true，管理端
        #  整个敞开。换成角色之后它和后端 `require_reader_admin` 是同一个判据，
        #  前端看到的和后端实际执行的不会再分叉。
        admin=bool(user and user.is_admin),
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
async def register(payload: RegisterRequest, request: Request, session: SessionDep) -> SessionState:
    """Create a reader account.

    Two paths, and which one applies depends only on whether any account exists:

    **First-run bootstrap** (no account yet). No invite code needed, and the
    account is created as ``ADMIN``. A configured admin password is required
    before the first account can be claimed; otherwise anyone on the LAN could
    permanently take over a fresh installation. It is deliberately **exempt** from
    ``[auth] register_open``: that flag closes self-service signup by
    strangers, while this is the operator claiming their own box with the admin
    password. Honouring it here would make ``register_open = false`` a box that
    can never have a first account at all.

    **Invite code** (an account exists). Requires a code issued by
    ``funread-api accounts invite``, and produces a ``GUEST``. The four reasons
    a code can be unusable (unknown / revoked / expired / exhausted) share one
    message -- splitting them turns this into a "does this code exist" probe.
    """
    try:
        username = validate_credentials(payload.username, payload.password)
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error

    is_first = await count_users(session) == 0
    if is_first:
        if not resolve_api_password():
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="请先配置管理口令，再注册首个账号",
            )
        if not is_authenticated(request):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="首个账号需要先通过管理口令验证（POST /auth/admin/login）",
            )
        try:
            user = await accounts.create_user(session, username, payload.password, UserRole.ADMIN)
        except UsernameTaken as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    else:
        if not registration_open():
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="注册未开放：服务端在 config.toml 里设了 [auth] register_open = false",
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
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error

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
    if (
        existing is not None
        and existing.is_active
        and is_legacy_hash(existing.password_hash)
        and verify_legacy_password(payload.password, existing.password_hash)
    ):
        #  升级前就有的账号：哈希还是 scrypt，bcrypt 验不了。自己验一次，成功就
        #  当场换成 bcrypt —— 每个账号最多走这一次，用户不用重设口令。
        #  不交给 funauth.authenticate 的原因是它只认 bcrypt，会直接当成口令错。
        await accounts.set_password(session, existing.username, payload.password)
        return _state(request, _login(request, existing))

    #  失败一律落到 funauth.authenticate 去报，**包括老账号验不过那种** —— 自己抛
    #  一句措辞不同的 401 就等于告诉探测方「这个账号是升级前建的」，而那是账号
    #  存在性的一次确认。scrypt 串在 bcrypt 下必然验不过（`verify_password` 对非法
    #  格式返回 False 而不是抛），所以这条路只会抛，不会把老账号意外放行。
    try:
        user = await accounts.authenticate(session, payload.username, payload.password)
    except BadCredentials as error:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(error)) from error
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


# ------------------------------------------------------------------ 人员管理
#
# 这一组是管理端的「人员」页：列出账号、改角色、停用启用。全部要求 ADMIN 身份
# （`require_reader_admin`），和 /sources、/pool 同一条标准 —— 能管人显然比能管
# 采集源更敏感。
#
# 注意 `/auth` 这个 router 整体**没有**全局守卫（它是登录入口，自己不能要求登录），
# 所以这里每个端点都要单独挂。


class UserOut(BaseModel):
    """人员列表的一行。不含任何口令字段。"""

    user_id: int
    username: str
    role: UserRole
    is_active: bool
    is_self: bool


class UserPatch(BaseModel):
    """改角色或改启用状态。两个都可选，只改传了的那个。"""

    role: UserRole | None = None
    is_active: bool | None = None


@router.get("/users", response_model=list[UserOut])
async def list_accounts(
    session: SessionDep, actor: CurrentUser = Depends(require_reader_admin)
) -> list[UserOut]:
    """列出所有账号。"""
    users = await accounts.list_users(session)
    return [
        UserOut(
            user_id=int(item.id),
            username=item.username,
            role=item.role,
            is_active=bool(item.is_active),
            is_self=int(item.id) == actor.user_id,
        )
        for item in users
    ]


@router.patch("/users/{user_id}", response_model=UserOut)
async def update_account(
    user_id: int,
    payload: UserPatch,
    session: SessionDep,
    actor: CurrentUser = Depends(require_reader_admin),
) -> UserOut:
    """改一个账号的角色或启用状态。

    两道闸，分别由两边把守：

    - **不能动自己**（这里拦）。降级或停用自己会让当前会话立刻失去管理权，而界面
      上通常是误点。要交接就先把对方提成 ADMIN，再由对方来降你。funauth 不管这条
      —— 它不知道谁是调用者。
    - **不能让在用的管理员归零**（funauth 拦）。`set_role` / `set_active` 撞上会抛
      `LastAdmin`，这里翻成 409。自己再算一遍没有意义：判定重复两份就会漂移，而
      funauth 那份还顺带覆盖了它自己的其它入口。

    走服务层而不是直接改 ORM 属性：后者绕过 funauth，以后它在角色变更上加审计或
    缓存失效，这里就挂不上。
    """
    user = await accounts.get_by_id(session, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="账号不存在")

    if int(user.id) == actor.user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="不能修改自己的角色或状态，请让另一个管理员来操作",
        )

    username = user.username
    try:
        #  两步都 `commit=False`，最后一次性提交 —— 同时改角色和状态时，中间
        #  提交会留下一个「降了级但还启用着」的瞬间状态。
        if payload.role is not None:
            await accounts.set_role(session, username, payload.role, commit=False)
        if payload.is_active is not None:
            await accounts.set_active(session, username, payload.is_active, commit=False)
        await session.commit()
    except LastAdmin as error:
        await session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error

    await session.refresh(user)

    return UserOut(
        user_id=int(user.id),
        username=user.username,
        role=user.role,
        is_active=bool(user.is_active),
        is_self=False,
    )


# ------------------------------------------------------------------ 设备 token
#
# 给套壳 / 原生客户端用的长期凭据。为什么需要它、为什么不是 JWT、为什么是
# sha256 而不是 bcrypt，都写在 `accounts.ReaderDeviceToken` 的 docstring 里。
#
# 这组端点**只认 cookie 会话**（require_user 在客户端是 Web 登录态），因为
# 签发 token 的那一刻用户必然是在浏览器里登录着的。


class DeviceTokenOut(BaseModel):
    """列表里的一行。**不含 token 本身** —— 明文只在创建时给一次。"""

    id: int
    device_name: str
    created_at: str
    last_seen_at: str


class DeviceTokenCreated(DeviceTokenOut):
    #: 明文 token，**只在这里出现这一次**。库里只存 sha256，丢了只能重新签发。
    token: str


class DeviceTokenIn(BaseModel):
    device_name: str = Field(default="", max_length=128)


class DeviceTokenIssueIn(DeviceTokenIn):
    """用账号口令直接换 token 的入参。"""

    username: str
    password: str


def _token_out(row: ReaderDeviceToken) -> DeviceTokenOut:
    return DeviceTokenOut(
        id=int(row.id),
        device_name=row.device_name or "",
        created_at=row.created_at.isoformat() if row.created_at else "",
        last_seen_at=row.last_seen_at.isoformat() if row.last_seen_at else "",
    )


@router.post("/tokens", response_model=DeviceTokenCreated, status_code=status.HTTP_201_CREATED)
async def create_device_token(
    payload: DeviceTokenIn,
    session: SessionDep,
    user: CurrentUser = Depends(require_user),
) -> DeviceTokenCreated:
    """签发一个设备 token。明文只返回这一次。"""
    raw = new_device_token()
    row = ReaderDeviceToken(
        token_hash=hash_device_token(raw),
        user_id=user.user_id,
        device_name=payload.device_name.strip(),
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return DeviceTokenCreated(**_token_out(row).model_dump(), token=raw)


@router.post(
    "/tokens/issue",
    response_model=DeviceTokenCreated,
    status_code=status.HTTP_201_CREATED,
)
async def issue_device_token(
    payload: DeviceTokenIssueIn, session: SessionDep
) -> DeviceTokenCreated:
    """用账号口令直接换一个设备 token，**不需要先建立会话**。

    `POST /auth/tokens` 要求 `require_user`，而那依赖会话 cookie —— 可打包版
    客户端正是因为跨站拿不到 cookie 才需要 token，于是那条路对它是死的：登录
    的 `Set-Cookie` 在 `allow_credentials=False` 下根本不会被保存，下一步签发
    就是 401。

    所以这里直接收口令换 token（等价于 OAuth 的 password grant）。安全性不低于
    登录本身：能拿到口令的人本来就能登录，而换来的 token 可以单独吊销、还留下
    一条「哪台设备、什么时候」的记录。

    口令校验直接委托 funauth，失败一律同一个 401 —— 和 `/auth/login` 一致，
    不区分「账号不存在」和「口令错」，否则就是一次账号存在性确认。

    **不处理 scrypt 老账号**：那条升级路径在 `/auth/login` 里，走一次浏览器登录
    就换成 bcrypt 了。在这里再抄一遍逻辑，等于把同一件事的两个实现放在两处。
    """
    try:
        user = await accounts.authenticate(
            session, (payload.username or "").strip(), payload.password
        )
    except BadCredentials as error:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(error)) from error

    raw = new_device_token()
    row = ReaderDeviceToken(
        token_hash=hash_device_token(raw),
        user_id=int(user.id),
        device_name=payload.device_name.strip(),
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return DeviceTokenCreated(**_token_out(row).model_dump(), token=raw)


@router.get("/tokens", response_model=list[DeviceTokenOut])
async def list_device_tokens(
    session: SessionDep, user: CurrentUser = Depends(require_user)
) -> list[DeviceTokenOut]:
    """列出自己名下的设备。只看得到自己的 —— 这不是管理端功能。"""
    rows = await session.scalars(
        sa.select(ReaderDeviceToken)
        .where(ReaderDeviceToken.user_id == user.user_id)
        .order_by(ReaderDeviceToken.id)
    )
    return [_token_out(row) for row in rows]


@router.delete("/tokens/{token_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def revoke_device_token(
    token_id: int, session: SessionDep, user: CurrentUser = Depends(require_user)
) -> None:
    """吊销一台设备。手机丢了就用这个。

    按 `(id, user_id)` 删而不是只按 id：否则知道别人 token 的 id 就能把它删掉。
    """
    row = await session.scalar(
        sa.select(ReaderDeviceToken).where(
            ReaderDeviceToken.id == token_id,
            ReaderDeviceToken.user_id == user.user_id,
        )
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="没有这个设备")
    await session.delete(row)
    await session.commit()
