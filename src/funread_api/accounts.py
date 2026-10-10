"""阅读端账号：funauth 的表长在本服务自己的 `Base` 上。

## 为什么账号表在 funread-api 而不在 funread 核心库

funauth 要求 **Python 3.12 + SQLAlchemy async**，而 `funread` 的下限是 3.10、
`funread.legado.reader` 整层是同步的。把 funauth 拉成 funread 的硬依赖等于替所有
只用采集功能的人抬高 Python 下限，还要给那一层塞一个用不到的异步连接池。所以
账号留在这个本来就要 3.12 的服务里，核心库只认 `user_id` 这个整数。

代价是账号表和书架表在同一个库里却属于两个 metadata，所以**没有**指向
`reader_user.id` 的外键。本来也没有 —— `reader_shelf` 之类从一开始就只存一个裸
`user_id`（见 `funread.legado.reader.storage` 的模块 docstring）。

## 为什么主键改成整型自增

funauth 默认 `PkType = sa.Uuid`。但 funread 的书架 / 进度 / 订阅 / 文章状态四张表
的**复合主键**里已经是整型 `user_id`，`LOCAL_USER_ID = 0`（「还没有任何账号」时
的隐式身份）也依赖整型。换成 UUID 要重建那四张表、改掉服务层和前端的全部类型，
换来的只是主键形状好看。所以这里覆盖 `id` 这一列，并把 `parse_user_id` 与出参
模型一起换成整型 —— funauth 明确支持这条路（见 `make_user_deps` 的文档），只强调
两处必须对得上，这里由 `parse_user_id` / `ReaderUserOut` 对应。

自增从 1 起（SQLite 与 MySQL 都是），不会和 `LOCAL_USER_ID = 0` 撞。

## 两个引擎，一个库

DDL 和 funread 那边的同步查询走同步引擎（直接复用它的引擎缓存），funauth 的
账号读写走异步引擎。同一个库两个连接池不是最优，但另一条路是把整个阅读服务层
改成异步 —— 那是一次远大于本次改动的重写。SQLite 下靠 WAL 让「一写多读」并存，
PRAGMA 和同步侧设的是同一套。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
from pathlib import Path
from typing import Any, AsyncIterator, Optional

import sqlalchemy as sa
from funauth import Accounts, InviteCodeMixin, TimestampMixin, UserMixin, UserRole
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.pool import NullPool

from funread.base.config import is_sqlite_url
from funread.legado.manage.source.storage import _get_database_url, _get_engine

logger = logging.getLogger("funread_api")


class AuthBase(DeclarativeBase):
    """账号表的 declarative base。

    和 `ReaderBase` 分开：`init_reader_db()` 不该顺手建账号表，反过来也一样。
    两组表在同一个库里，由各自的 `init_*` 负责建。
    """


class ReaderUser(UserMixin, TimestampMixin, AuthBase):
    """一个阅读端账号。列定义来自 `funauth.UserMixin`，只换主键类型。

    和 B 端 `/admin` 的单口令完全分开 —— 管理端不该和读者账号共用凭据。
    口令由 funauth 用 bcrypt 哈希，明文不落库也不进日志。
    """

    __tablename__ = "reader_user"
    #: 唯一约束由宿主命名：funauth 的 mixin 不替宿主起约束名，否则两个用了
    #: 同一个 mixin 的库在同一个 PostgreSQL 实例里会撞名。
    __table_args__ = (sa.UniqueConstraint("username", name="uq_reader_user_username"),)

    #: 覆盖 `UserMixin` 的 UUID 主键，理由见模块 docstring。
    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)


class ReaderInviteCode(InviteCodeMixin, TimestampMixin, AuthBase):
    """一张邀请码。签发 / 消耗 / 吊销的逻辑都在 funauth 里。

    取代了原先的 `FUNREAD_REGISTER_CODE`：那是一个全局静态口令，泄露了就等于
    注册常开，而且撤销它会让所有在途的邀请一起失效。现在每张码有自己的次数、
    有效期和备注，吊销一张不影响其他。
    """

    __tablename__ = "reader_invite_code"
    __table_args__ = (sa.UniqueConstraint("code", name="uq_reader_invite_code_code"),)

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)


#: 账号服务单例。模型类由宿主声明并在这里绑上去，funauth 自己不建表。
accounts = Accounts(user_model=ReaderUser, invite_model=ReaderInviteCode)


class ReaderUserOut(BaseModel):
    """账号出参。`id` 是整型，必须和 `parse_user_id` 对得上。"""

    id: int
    username: str
    role: UserRole

    model_config = {"from_attributes": True}


# --------------------------------------------------------------- 异步引擎

#: 异步驱动映射。键是同步 URL 的 drivername，值是对应的异步 drivername。
#: `asyncpg` 不是本服务的硬依赖（它要编译扩展，而本地开发只用 SQLite、线上是
#: MySQL），所以 PostgreSQL 这条映射写在这里但驱动要自己装 —— 装之前 URL
#: 翻译照样正确，只是建引擎时 SQLAlchemy 会抛「找不到 asyncpg」。
_ASYNC_DRIVERS = {
    "sqlite": "sqlite+aiosqlite",
    "sqlite+pysqlite": "sqlite+aiosqlite",
    "mysql": "mysql+aiomysql",
    "mysql+pymysql": "mysql+aiomysql",
    "mysql+mysqldb": "mysql+aiomysql",
    "postgresql": "postgresql+asyncpg",
    "postgresql+psycopg2": "postgresql+asyncpg",
}

#: 已经是异步驱动的，原样放过。只对 URL 里**写明**的驱动生效，见 `to_async_url`。
_ALREADY_ASYNC = frozenset({"aiosqlite", "aiomysql", "asyncmy", "asyncpg", "psycopg"})

_ASYNC_ENGINES: dict[str, AsyncEngine] = {}
_ASYNC_SESSION_FACTORIES: dict[str, async_sessionmaker] = {}
_AUTH_INITIALIZED: set[str] = set()


def to_async_url(database_url: str) -> str:
    """把同步 URL 换成等价的异步 URL。

    「已经是异步」只看 URL 里**写明**的驱动（`dialect+driver://`），不用
    `url.get_driver_name()` —— 后者在没写驱动时会替你猜一个默认值，而这个默认值
    随 SQLAlchemy 版本变：2.1 把 `postgresql://` 的默认驱动从 psycopg2 换成了
    psycopg3，而后者本身就能异步。照它判断的话，同一个 `postgresql://` 在 2.0
    上会被映射成 asyncpg、在 2.1 上原样放过 —— 翻译结果取决于装了哪个版本。

    Raises:
        ValueError: 没有对应的异步驱动。宁可启动时就报出来，也不要让它在第一次
            登录时才变成一个看不懂的 500。
    """
    url = sa.engine.make_url(database_url)
    if url.drivername.partition("+")[2] in _ALREADY_ASYNC:
        return str(url)
    mapped = _ASYNC_DRIVERS.get(url.drivername)
    if mapped is None:
        raise ValueError(
            f"数据库 {url.drivername} 没有可用的异步驱动，账号功能需要它。"
            f"支持的同步前缀：{', '.join(sorted(_ASYNC_DRIVERS))}"
        )
    return str(url.set(drivername=mapped))


def _tune_sqlite(dbapi_conn: Any, _record: Any) -> None:
    """和同步侧同一套 PRAGMA。

    两个引擎开同一个 SQLite 文件，WAL 必须两边都开 —— 只在一边开的话另一边
    还是会把整库锁住，而账号表的写入（登录改不了什么，但注册会写）就会把正在
    读书架的请求挡成 "database is locked"。
    """
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


def get_async_engine(database_url: Optional[str] = None) -> AsyncEngine:
    """缓存一个异步引擎，按解析后的 URL 记。

    SQLite 下用 `NullPool`（即连即断），MySQL 保持默认的连接池。两个理由：

    - aiosqlite 的每条连接是一个真线程，池里留着就是留着线程；而打开一个
      SQLite 文件几乎不要钱，池化省不下什么。
    - 池化的连接是在「创建它的那个事件循环」里活着的。线上 uvicorn 一个循环跑
      到底，没问题；但测试里每个 `TestClient` 上下文都新开一个循环，复用上一个
      循环里的连接会以各种难懂的方式炸。MySQL 那边建连接贵得多，值得池化，而
      它也不是测试会走的路径。
    """
    resolved = _get_database_url(database_url)
    engine = _ASYNC_ENGINES.get(resolved)
    if engine is None:
        sqlite = is_sqlite_url(resolved)
        engine = create_async_engine(
            to_async_url(resolved),
            future=True,
            **({"poolclass": NullPool} if sqlite else {}),
        )
        if sqlite:
            sa.event.listen(engine.sync_engine, "connect", _tune_sqlite)
        _ASYNC_ENGINES[resolved] = engine
    return engine


def get_async_session_factory(database_url: Optional[str] = None) -> async_sessionmaker:
    resolved = _get_database_url(database_url)
    factory = _ASYNC_SESSION_FACTORIES.get(resolved)
    if factory is None:
        factory = async_sessionmaker(
            bind=get_async_engine(resolved), expire_on_commit=False, autoflush=False
        )
        _ASYNC_SESSION_FACTORIES[resolved] = factory
    return factory


async def reset_async_engines() -> None:
    """丢掉所有缓存的异步引擎。测试在换库之间调它。

    必须 `dispose()` 而不是只清字典：aiosqlite 的连接跑在自己的线程里，不关掉
    会在测试进程里堆出几百个线程，最后以 "cannot schedule new futures" 收场。
    """
    for engine in list(_ASYNC_ENGINES.values()):
        await engine.dispose()
    _ASYNC_ENGINES.clear()
    _ASYNC_SESSION_FACTORIES.clear()
    _AUTH_INITIALIZED.clear()


# --------------------------------------------------------------- 建表与迁移

#: 旧账号表搬走前的临时名。
_LEGACY_USER_TABLE = "reader_user__pre_funauth"


def _migrate_legacy_user_table(engine: Any) -> None:
    """把 M3d 时建的 `reader_user`（整型 `user_id` 主键 + `disabled`）换成新形状。

    改的是主键列名和一个布尔列的极性，SQLite 都改不了，所以走标准重建三步：
    旧表改名 → 按新形状建表 → 带着映射搬数据。`user_id` 原值搬进 `id`，所以
    书架 / 进度里那些 `user_id` 引用一行都不用动。

    `disabled` → `is_active` 取反；`role` 一律 `guest`（管理员要显式建，见
    `funread-api accounts create-admin`）。口令哈希**原样搬过去**：旧的是
    `scrypt$...`，funauth 用 bcrypt 验不了它，所以登录时走一次透明升级
    （见 `verify_legacy_password`），用户不用重设口令。
    """
    inspector = sa.inspect(engine)
    if "reader_user" not in set(inspector.get_table_names()):
        return
    columns = {column["name"] for column in inspector.get_columns("reader_user")}
    if "user_id" not in columns:
        return

    with engine.begin() as connection:
        before = connection.execute(sa.text("SELECT COUNT(*) FROM reader_user")).scalar_one()
        #  上一次迁移中途挂掉留下的残渣，先清掉再重来
        connection.execute(sa.text(f"DROP TABLE IF EXISTS {_LEGACY_USER_TABLE}"))
        connection.execute(sa.text(f"ALTER TABLE reader_user RENAME TO {_LEGACY_USER_TABLE}"))

    ReaderUser.__table__.create(engine)

    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO reader_user"
                " (id, username, password_hash, role, is_active, created_at, updated_at)"
                " SELECT user_id, username, password_hash, 'guest',"
                #  CASE 而不是 NOT disabled：MySQL 和 SQLite 对布尔取反的写法
                #  不完全一致，CASE 两边都认
                "        CASE WHEN disabled THEN 0 ELSE 1 END,"
                "        created_at, updated_at"
                f" FROM {_LEGACY_USER_TABLE}"
            )
        )
        after = connection.execute(sa.text("SELECT COUNT(*) FROM reader_user")).scalar_one()
        if after != before:
            raise RuntimeError(
                f"reader_user 迁移后行数不符：迁移前 {before}，迁移后 {after}。"
                f"旧数据仍在 {_LEGACY_USER_TABLE}，没有删除。"
            )
        connection.execute(sa.text(f"DROP TABLE {_LEGACY_USER_TABLE}"))

    if before:
        logger.info(
            f"reader_user 已迁到 funauth 形状，{before} 个账号保留原 user_id；"
            f"旧 scrypt 口令会在下次登录时自动换成 bcrypt"
        )


def init_auth_db(database_url: Optional[str] = None) -> None:
    """建账号表（缺了才建）并跑一次迁移。

    DDL 走**同步**引擎：它本来就为 funread 那层存在，而建表这种一次性操作没有
    任何理由再开一条异步连接。
    """
    engine = _get_engine(database_url)
    key = str(engine.url)
    if key in _AUTH_INITIALIZED:
        return
    #  先迁移再 create_all：create_all 只跳过已存在的表，不会去改它的形状
    _migrate_legacy_user_table(engine)
    AuthBase.metadata.create_all(engine)
    _AUTH_INITIALIZED.add(key)


async def get_session(database_url: Optional[str] = None) -> AsyncIterator[AsyncSession]:
    """FastAPI 的会话依赖。异常时 rollback —— funauth 明确把回滚留给宿主。

    注册那条路径依赖这个 rollback：用户名撞车时 `register_with_invite` 已经扣掉
    的邀请码名额必须跟着回滚，否则别人手滑输了个重名用户名，这张码就白少一次。
    """
    init_auth_db(database_url)
    factory = get_async_session_factory(database_url)
    session = factory()
    try:
        yield session
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def count_users(session: AsyncSession) -> int:
    """账号总数。0 表示「还没有任何账号」，隐式本地身份只在那时可用。"""
    return int(await session.scalar(sa.select(sa.func.count()).select_from(ReaderUser)) or 0)


# --------------------------------------------------------------- 入参规则

#: 用户名规则：字母数字加下划线短横点，3-32 位。funauth 自己不校验格式（它只保证
#: 唯一），限死是为了让用户名能安全地出现在日志、URL 和错误文案里，不必再逐处转义。
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,31}$")

#: 口令下限。比 funauth `RegisterPayload` 的 6 位严 —— 那是包的底线，不是本服务的策略。
MIN_PASSWORD_LENGTH = 8


def validate_credentials(username: str, password: str) -> str:
    """校验用户名与口令，返回去空白后的用户名。

    建账号的三条路（自助注册、首次引导、CLI）都过这里，所以规则只有一处。

    Raises:
        ValueError: 用户名不合规或口令太短。调用方把它翻成 400。
    """
    cleaned = (username or "").strip()
    if not USERNAME_PATTERN.match(cleaned):
        raise ValueError("用户名需为 3-32 位字母、数字、下划线、短横线或点，且以字母数字开头")
    if len(password or "") < MIN_PASSWORD_LENGTH:
        raise ValueError(f"口令至少 {MIN_PASSWORD_LENGTH} 位")
    return cleaned


# --------------------------------------------------------------- 旧口令透明升级

#: 旧实现的哈希串前缀。funauth 用 bcrypt，验不了它。
_LEGACY_SCHEME = "scrypt"


def verify_legacy_password(password: str, stored: str) -> bool:
    """验一个 `scrypt$n$r$p$salt$key` 旧哈希。永不抛：格式坏了就是验证失败。

    保留这段 stdlib scrypt 只为了一件事：让升级前就有账号的人不用重设口令。
    登录成功后 `v1/auth.py` 会立刻用 bcrypt 重写哈希，所以每个账号最多走这条
    路一次。等所有部署都登过一轮，这个函数和 `_LEGACY_SCHEME` 就可以删掉。
    """
    try:
        scheme, n, r, p, salt_hex, key_hex = (stored or "").split("$")
        if scheme != _LEGACY_SCHEME:
            return False
        candidate = hashlib.scrypt(
            (password or "").encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(bytes.fromhex(key_hex)),
            maxmem=64 * 1024 * 1024,
        )
    except (ValueError, TypeError, MemoryError):
        return False
    #  compare_digest，不是 ==：普通比较会按时间泄露哈希
    return hmac.compare_digest(candidate.hex(), key_hex)


def is_legacy_hash(stored: Optional[str]) -> bool:
    return bool(stored) and str(stored).startswith(f"{_LEGACY_SCHEME}$")


# --------------------------------------------------------------- 会话密钥

#: `SessionMiddleware` 的签名密钥文件名。放在配置目录里，不进仓库。
SESSION_SECRET_FILENAME = "session-secret"


def _config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "farfarfun" / "funread-api"


def resolve_session_secret() -> str:
    """env `FUNREAD_SESSION_SECRET` > funsecret > 配置目录里的一个文件。

    **不能用「每次启动随机生成」**（funflix 那样）：阅读端的会话有效期是一个月，
    而这个服务会因为升级、改配置、机器重启而重启好几次。每次重启都把所有人踢下线，
    对一个用手机打开的看书应用来说是实打实的故障。

    所以兜底这条路会把密钥**落盘一次**（0600），之后每次启动读同一份。文件在
    配置目录里而不是工作目录里 —— 工作目录可能是个临时 checkout。
    """
    env_value = os.environ.get("FUNREAD_SESSION_SECRET")
    if env_value:
        return env_value

    try:
        from funsecret import read_secret

        value = read_secret(cate1="funread", cate2="api", cate3="auth", cate4="session_secret")
        if value:
            return str(value)
    except Exception:
        #  没配 funsecret 是新克隆和 CI 的常态，不是错误
        pass

    path = _config_dir() / SESSION_SECRET_FILENAME
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except OSError:
        pass

    secret = secrets.token_hex(32)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secret, encoding="utf-8")
        path.chmod(0o600)
        logger.info(f"已生成会话签名密钥：{path}")
    except OSError as error:
        #  只读文件系统之类。还是能跑，只是重启后大家要重新登录 —— 说清楚，
        #  别让人以为是 bug。
        logger.warning(f"会话签名密钥写不进 {path}（{error}），本次启动用临时密钥，重启后需重新登录")
    return secret


def registration_open() -> bool:
    """注册入口开不开。`FUNREAD_REGISTER_OPEN` 默认**开**。

    和旧的 `FUNREAD_REGISTER_CODE` 相反（那个默认关）。因为闸门换了：旧方案的
    「码」是一个环境变量里的静态口令，开着就等于谁都能注册；现在要的是库里一张
    真实存在、还有剩余次数的邀请码，**一张都没签发时注册实际上就是关着的**。
    默认关反而会让人签发了码却注册不了，还查不出为什么。

    真要彻底封死（比如公网暴露期间）就设 `FUNREAD_REGISTER_OPEN=0`。
    """
    value = os.environ.get("FUNREAD_REGISTER_OPEN", "").strip().lower()
    if not value:
        return True
    return value in {"1", "true", "yes", "on"}


__all__ = [
    "AuthBase",
    "ReaderInviteCode",
    "ReaderUser",
    "MIN_PASSWORD_LENGTH",
    "ReaderUserOut",
    "USERNAME_PATTERN",
    "UserRole",
    "accounts",
    "count_users",
    "get_async_engine",
    "get_async_session_factory",
    "get_session",
    "init_auth_db",
    "is_legacy_hash",
    "registration_open",
    "reset_async_engines",
    "resolve_session_secret",
    "to_async_url",
    "validate_credentials",
    "verify_legacy_password",
]
