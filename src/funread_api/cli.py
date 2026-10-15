"""Command-line entry point for the funread API service.

Settings resolution, highest priority first:

1. an explicit CLI flag (``--host`` / ``--port``)
2. the config file (``--config``, else the default path below)
3. the shared root config, ``~/.farfarfun/funread/config.toml``
4. the hardcoded loopback defaults

**No ``FUNREAD_*`` environment variable is read any more** -- see the module
docstring of ``funread.base.config`` for why, and for the single four-layer
chain (explicit > config.toml > funsecret > fallback) every setting now shares.
Anything that used to be an env var belongs in the config file; this CLI prints
a loud notice for the ones it finds still set.

The config file defaults to ``~/.farfarfun/funread/api/config.toml`` (see
``paths.py`` for why that is not under ``XDG_CONFIG_HOME``) and is parsed by
extension (``.toml`` / ``.json`` / ``.env``). It is *registered* with
``funread.base.config`` so sections it shares with the rest of the family --
``[database]``, ``[cache]``, ``[auth]`` -- take priority over the shared root
file. Runtime state -- the PID and log files -- lives *next to whichever config
actually resolved*, not in a fixed directory and not relative to the current
directory: a host running two instances off two configs keeps two independent
sets of state, and ``stop`` works from anywhere.

Installs predating 1.1.114 kept all of this under
``${XDG_CONFIG_HOME:-~/.config}/farfarfun/funread-api/``; the config file and
the session secret are moved across on first run, see
``paths.migrate_legacy_state``.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Annotated

import typer

from funread.base import config as shared_config
from funread_api.paths import default_config_path, migrate_legacy_state

PACKAGE_NAME = "funread-api"
CLI_NAME = "funread-api"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18811
STOP_TIMEOUT_SECONDS = 10
START_TIMEOUT_SECONDS = 5

#: 监听地址在配置文件里的候选位置，按先后顺序找。四种写法都认：服务自己那份
#: config.toml 的 ``[server]`` 段、它的顶层键、共享根文件的 ``[api]`` 段，以及
#: ``.env`` 里的 ``FUNREAD_API_HOST=``（扁平键，``funread_`` 前缀会被剥掉）。
#: 每份文件都按这个顺序找一遍，所以服务自己那份的任何写法都盖得住共享根文件。
_HOST_CANDIDATES = (("server", "host"), (None, "host"), ("api", "host"), (None, "api_host"))
_PORT_CANDIDATES = (("server", "port"), (None, "port"), ("api", "port"), (None, "api_port"))

#: 本服务自己那几个已废弃的环境变量 -> 现在该写哪个键。核心库那两个由
#: ``shared_config.DEPRECATED_ENV_VARS`` 提供，启动时两张表并起来一起检查。
_DEPRECATED_ENV_VARS = {
    "FUNREAD_API_HOST": "config.toml 的 [server] host",
    "FUNREAD_API_PORT": "config.toml 的 [server] port",
    "FUNREAD_API_PASSWORD": "config.toml 的 [auth] password",
    "FUNREAD_SESSION_SECRET": "config.toml 的 [auth] session_secret",
    "FUNREAD_READER_PUBLIC": "config.toml 的 [auth] reader_public",
    "FUNREAD_REGISTER_OPEN": "config.toml 的 [auth] register_open",
}


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Settings:
    """Everything the service needs, plus where its runtime state belongs."""

    config_path: Path
    host: str
    port: int

    @property
    def state_dir(self) -> Path:
        return self.config_path.parent

    @property
    def pid_file(self) -> Path:
        return self.state_dir / f"{CLI_NAME}.pid"

    @property
    def log_file(self) -> Path:
        return self.state_dir / f"{CLI_NAME}.log"


def _default_config_path() -> Path:
    return default_config_path()


def _coerce_port(value: object, *, source: str) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise RuntimeError(f"invalid port from {source}: {value!r}") from None


def _register_config(config: str | None) -> Path:
    """定下这次用哪份配置文件，并把它注册进 ``funread.base.config`` 的查找链。

    注册必须发生在任何 ``resolve_*`` 之前，否则 ``[database]``、``[auth]`` 这些跨
    进程共享的段只会从共享根文件里读到，服务自己那份 config.toml 被整个跳过。

    幂等：``use_config_file`` 对重复路径是空操作，``migrate_legacy_state`` 第二次
    返回空列表，所以同一进程里调多次没有副作用。
    """
    if config:
        path = Path(config).expanduser()
    else:
        # 只在走默认路径时迁移：显式 --config 指的是用户自己管的文件，不该被动过。
        # 放在读配置之前 —— 旧位置那份 config.toml 得先搬过来才读得到。
        for name in migrate_legacy_state():
            print(f"已从旧配置目录迁移 {name} -> {default_config_path().parent}")
        path = _default_config_path()
    shared_config.use_config_file(path)
    return path


def _resolve_settings(
    *,
    config: str | None = None,
    host: str | None = None,
    port: int | None = None,
) -> Settings:
    """Fold the CLI flags, the config files and the defaults.

    ``host`` and ``port`` are ``None`` when the flag was not given -- commands
    that have no ``--host`` flag at all (``stop`` / ``status`` / ``uninstall``)
    simply leave it out.
    """
    config_path = _register_config(config)
    # 文件不存在只在用户点了名的时候算错误：新装的机器上默认路径没有文件是常态。
    if config is not None and not config_path.exists():
        raise RuntimeError(f"config file not found: {config_path}")

    if host is None:
        host = shared_config.config_value(*_HOST_CANDIDATES) or DEFAULT_HOST

    # `--port` 已经被 Typer 的类型标注验过了，不再过 _coerce_port —— 否则一个命令行
    # 上的错值会被报成"配置文件里的端口不对"，指错地方。
    if port is None:
        port_value, origin = shared_config.config_value_with_origin(*_PORT_CANDIDATES)
        # origin 可能是服务自己那份，也可能是共享根文件，所以报错用命中的那份而不是
        # config_path。
        port = (
            DEFAULT_PORT
            if port_value is None
            else _coerce_port(port_value, source=f"config file {origin}")
        )

    return Settings(config_path=config_path, host=str(host), port=int(port))


# ---------------------------------------------------------------------------
# PID bookkeeping
# ---------------------------------------------------------------------------


def _read_pid(settings: Settings) -> int | None:
    try:
        pid = int(settings.pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return pid if pid > 1 else None


def _pid_is_live(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        return Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()[2] != "Z"
    except (OSError, IndexError):
        return True


def _pid_belongs_to_service(pid: int) -> bool:
    try:
        command = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False
    return b"funread_api.cli" in command


def _clear_state(settings: Settings) -> None:
    settings.pid_file.unlink(missing_ok=True)


def _installed_version() -> str:
    try:
        return version(PACKAGE_NAME)
    except PackageNotFoundError:
        return "unknown"


# ---------------------------------------------------------------------------
# Service lifecycle
# ---------------------------------------------------------------------------


def _run_server(settings: Settings) -> int:
    existing = _read_pid(settings)
    if existing is not None and existing != os.getpid() and _pid_is_live(existing):
        raise RuntimeError(f"{CLI_NAME} is already running (pid {existing})")

    settings.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    settings.pid_file.write_text(f"{os.getpid()}\n", encoding="utf-8")
    try:
        import uvicorn

        uvicorn.run(
            "funread_api.app:create_app",
            factory=True,
            host=settings.host,
            port=settings.port,
        )
    finally:
        _clear_state(settings)
    return 0


def _start_server(settings: Settings) -> int:
    existing = _read_pid(settings)
    if existing is not None and _pid_is_live(existing):
        raise RuntimeError(f"{CLI_NAME} is already running (pid {existing})")
    _clear_state(settings)

    settings.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    # The child re-resolves settings, so hand it the config path too -- otherwise
    # it would write its PID file next to a *different* config than the parent
    # just checked.
    command = [
        sys.executable,
        "-m",
        "funread_api.cli",
        "server",
        "run",
        "--config",
        str(settings.config_path),
        "--host",
        settings.host,
        "--port",
        str(settings.port),
    ]
    with settings.log_file.open("ab") as log:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    deadline = time.monotonic() + START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        pid = _read_pid(settings)
        if process.poll() is not None:
            break
        if pid is not None and _pid_is_live(pid):
            print(
                f"{CLI_NAME} {_installed_version()} started "
                f"(pid {pid}, {settings.host}:{settings.port})"
            )
            return 0
        time.sleep(0.1)
    _clear_state(settings)
    raise RuntimeError(f"{CLI_NAME} failed to start; see {settings.log_file}")


def _kill_by_port(port: int) -> list[int]:
    """Kill whatever holds ``port``, via funshell. Returns the PIDs it killed.

    This is the fallback for a lost or stale PID file -- including state written
    by older versions, which put the PID file under the *current directory*, so
    a stop from elsewhere could not find it.
    """
    try:
        from funshell.kill import kill_process
    except ImportError:
        raise RuntimeError(
            "funshell is required to stop a service whose PID file is missing"
        ) from None
    # SIGTERM, not SIGKILL: uvicorn needs to run its shutdown handlers.
    results = kill_process(port=port, sig=str(int(signal.SIGTERM)))
    return [pid for pid, killed in results or () if killed]


def _stop_server(settings: Settings) -> int:
    pid = _read_pid(settings)
    if pid is not None and _pid_is_live(pid):
        if not _pid_belongs_to_service(pid):
            raise RuntimeError(f"PID {pid} is not a {CLI_NAME} service; refusing to stop it")
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + STOP_TIMEOUT_SECONDS
        while _pid_is_live(pid):
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"{CLI_NAME} did not stop within {STOP_TIMEOUT_SECONDS}s (pid {pid})"
                )
            time.sleep(0.1)
        _clear_state(settings)
        print(f"{CLI_NAME} stopped (pid {pid})")
        return 0

    _clear_state(settings)
    killed = _kill_by_port(settings.port)
    if killed:
        joined = ", ".join(str(pid) for pid in killed)
        print(f"{CLI_NAME} stopped via port {settings.port} (pid {joined})")
        return 0
    print(f"{CLI_NAME} is not running")
    return 0


def _status_server(settings: Settings) -> int:
    installed = _installed_version()
    pid = _read_pid(settings)
    if pid is not None and _pid_is_live(pid):
        print(
            f"{CLI_NAME} {installed}: running "
            f"(pid {pid}, {settings.host}:{settings.port}, config {settings.config_path})"
        )
        return 0
    if pid is not None:
        _clear_state(settings)
    print(f"{CLI_NAME} {installed}: not running (config {settings.config_path})")
    return 0


# ---------------------------------------------------------------------------
# Package lifecycle
# ---------------------------------------------------------------------------


def _run_uv(arguments: list[str]) -> int:
    executable = shutil.which("uv")
    if executable is None:
        raise RuntimeError(f"uv is required to manage an installed {PACKAGE_NAME} package")
    return subprocess.run([executable, *arguments], check=False).returncode


def _upgrade(version_name: str | None) -> int:
    target = f"{PACKAGE_NAME}=={version_name}" if version_name else PACKAGE_NAME
    return _run_uv(["tool", "install", "--reinstall", "--upgrade", target])


def _rollback(version_name: str) -> int:
    return _run_uv(["tool", "install", "--reinstall", f"{PACKAGE_NAME}=={version_name}"])


def _uninstall(settings: Settings) -> int:
    _stop_server(settings)
    return _run_uv(["tool", "uninstall", PACKAGE_NAME])


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------
#
# ``add_completion=False`` keeps the option surface to exactly the flags below,
# and ``pretty_exceptions_enable=False`` keeps an unexpected crash a plain
# traceback: ``server start``'s stderr goes straight into the log file, where
# Typer's framed and coloured one would only be harder to read.

app = typer.Typer(
    help="Manage the funread API service",
    add_completion=False,
    pretty_exceptions_enable=False,
)
server_app = typer.Typer(help="manage the API service")
accounts_app = typer.Typer(help="manage reader accounts and invite codes")
invite_app = typer.Typer(help="manage registration invite codes")
app.add_typer(server_app, name="server")
app.add_typer(accounts_app, name="accounts")
accounts_app.add_typer(invite_app, name="invite")

ConfigOption = Annotated[
    str | None,
    typer.Option(
        "--config",
        metavar="PATH",
        help=(
            "config file (.toml/.json/.env); defaults to "
            "~/.farfarfun/funread/api/config.toml. "
            "The PID and log files live in the same directory."
        ),
    ),
]
# Default None, not DEFAULT_HOST/PORT: _resolve_settings has to be able to tell
# "the user passed this" from "fall through to the config file".
HostOption = Annotated[str | None, typer.Option("--host")]
PortOption = Annotated[int | None, typer.Option("--port")]


def _dispatch(action: Callable[[], int]) -> None:
    """Run a command body and turn its result into this process' exit code.

    Every command funnels through here so that a ``RuntimeError`` -- the way
    the helpers above report an expected failure -- stays a one-line ``error:``
    on stderr with exit status 1 rather than a traceback.
    """
    try:
        code = action()
    except RuntimeError as error:
        print(f"error: {error}", file=sys.stderr)
        raise typer.Exit(1) from error
    if code:
        raise typer.Exit(code)


@server_app.command("run")
def server_run(
    config: ConfigOption = None, host: HostOption = None, port: PortOption = None
) -> None:
    """Run the service in the foreground."""
    _dispatch(lambda: _run_server(_resolve_settings(config=config, host=host, port=port)))


@server_app.command("start")
def server_start(
    config: ConfigOption = None, host: HostOption = None, port: PortOption = None
) -> None:
    """Start the service in the background."""
    _dispatch(lambda: _start_server(_resolve_settings(config=config, host=host, port=port)))


@server_app.command("restart")
def server_restart(
    config: ConfigOption = None, host: HostOption = None, port: PortOption = None
) -> None:
    """Stop the background service, then start it again."""

    def action() -> int:
        settings = _resolve_settings(config=config, host=host, port=port)
        _stop_server(settings)
        return _start_server(settings)

    _dispatch(action)


# stop/status take --config and --port but no --host: the former locates the PID
# file, the latter is the fallback when that file is gone.
@server_app.command("stop")
def server_stop(config: ConfigOption = None, port: PortOption = None) -> None:
    """Stop the background service."""
    _dispatch(lambda: _stop_server(_resolve_settings(config=config, port=port)))


@server_app.command("status")
def server_status(config: ConfigOption = None, port: PortOption = None) -> None:
    """Report whether the service is running."""
    _dispatch(lambda: _status_server(_resolve_settings(config=config, port=port)))


# Deliberately absent: an `install` command. A CLI cannot install itself before
# it exists -- that is `scripts/setup.sh`'s or `uv tool install`'s job.
@app.command("upgrade")
def upgrade(version: Annotated[str | None, typer.Argument()] = None) -> None:
    """upgrade to the latest or a named version"""
    _dispatch(lambda: _upgrade(version))


@app.command("rollback")
def rollback(version: Annotated[str, typer.Argument()]) -> None:
    """install a specific earlier version"""
    _dispatch(lambda: _rollback(version))


@app.command("uninstall")
def uninstall(config: ConfigOption = None, port: PortOption = None) -> None:
    """stop the service and uninstall the package"""
    _dispatch(lambda: _uninstall(_resolve_settings(config=config, port=port)))


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------
#
# Registration is invite-only once the first account exists, so *something*
# has to be able to mint the first admin and hand out codes without going
# through HTTP. That something is here rather than an HTTP endpoint on purpose:
# an endpoint that creates admins is an endpoint that can be found.
#
# Everything in this section imports funauth/SQLAlchemy lazily. `server stop`
# and `status` have to work when the database is unreachable -- and they are the
# two commands most likely to be run *because* it is.


def _prompt_password(confirm: bool = True) -> str:
    """Read a password from the tty. Never from argv -- that lands in shell history."""
    import getpass

    password = getpass.getpass("口令: ")
    if confirm and getpass.getpass("再输一次: ") != password:
        raise RuntimeError("两次输入的口令不一致")
    return password


async def _with_session(action):
    """Run ``action(session)`` against the account database, then dispose the engine.

    Disposing matters here and not in the server: aiosqlite runs each
    connection on its own thread, and a CLI process that exits with one still
    open hangs instead of returning to the shell.
    """
    from funread_api.accounts import (
        get_async_session_factory,
        init_auth_db,
        reset_async_engines,
    )

    init_auth_db()
    session_factory = get_async_session_factory()
    try:
        async with session_factory() as session:
            return await action(session)
    finally:
        await reset_async_engines()


def _run_account_action(action) -> int:
    """Run ``action(session)`` against the account database from sync code."""
    import asyncio

    return asyncio.run(_with_session(action))


UsernameArgument = Annotated[str, typer.Argument()]


@accounts_app.command("list")
def accounts_list() -> None:
    """list accounts"""
    from funread_api.accounts import accounts

    async def run(session) -> int:
        users = await accounts.list_users(session)
        if not users:
            print("还没有账号。先建一个：funread-api accounts create-admin <用户名>")
            return 0
        for user in users:
            flag = "" if user.is_active else "  [已停用]"
            print(f"{user.id:>5}  {user.username:<24} {user.role}{flag}")
        return 0

    _dispatch(lambda: _run_account_action(run))


@accounts_app.command("create-admin")
def accounts_create_admin(username: UsernameArgument) -> None:
    """create an admin account (password read from the tty)"""
    from funauth import UsernameTaken, UserRole

    from funread_api.accounts import accounts, validate_credentials

    async def run(session) -> int:
        password = _prompt_password()
        try:
            name = validate_credentials(username, password)
        except ValueError as error:
            raise RuntimeError(str(error)) from error
        try:
            user = await accounts.create_user(session, name, password, UserRole.ADMIN)
        except UsernameTaken as error:
            raise RuntimeError(str(error)) from error
        print(f"已创建管理员 {user.username}（id={user.id}）")
        return 0

    _dispatch(lambda: _run_account_action(run))


@accounts_app.command("set-password")
def accounts_set_password(username: UsernameArgument) -> None:
    """reset an account's password"""
    from funread_api.accounts import accounts, validate_credentials

    async def run(session) -> int:
        password = _prompt_password()
        try:
            validate_credentials(username, password)
        except ValueError as error:
            raise RuntimeError(str(error)) from error
        if not await accounts.set_password(session, username, password):
            raise RuntimeError(f"没有这个账号：{username}")
        print(f"已重设 {username} 的口令")
        return 0

    _dispatch(lambda: _run_account_action(run))


def _set_active(username: str, *, active: bool) -> None:
    from funread_api.accounts import accounts

    async def run(session) -> int:
        if not await accounts.set_active(session, username, active):
            raise RuntimeError(f"没有这个账号：{username}")
        print(f"已{'启用' if active else '停用'} {username}")
        return 0

    _dispatch(lambda: _run_account_action(run))


@accounts_app.command("enable")
def accounts_enable(username: UsernameArgument) -> None:
    """re-enable a disabled account"""
    _set_active(username, active=True)


@accounts_app.command("disable")
def accounts_disable(username: UsernameArgument) -> None:
    """disable an account without deleting it"""
    _set_active(username, active=False)


@invite_app.command("issue")
def invite_issue(
    max_uses: Annotated[int, typer.Option("--max-uses")] = 1,
    expires_in_days: Annotated[int | None, typer.Option("--expires-in-days")] = None,
    note: Annotated[str | None, typer.Option("--note")] = None,
) -> None:
    """issue a code; prints the code value"""
    from funread_api.accounts import accounts

    async def run(session) -> int:
        code = await accounts.issue_invite(
            session,
            max_uses=max_uses,
            expires_in_days=expires_in_days,
            note=note,
        )
        print(code.code)
        return 0

    _dispatch(lambda: _run_account_action(run))


@invite_app.command("list")
def invite_list() -> None:
    """list codes with their remaining uses"""
    from funread_api.accounts import accounts

    async def run(session) -> int:
        invites = await accounts.list_invites(session)
        if not invites:
            print("还没有邀请码。签一张：funread-api accounts invite issue")
            return 0
        for invite in invites:
            status_text = accounts.describe_invite_status(invite)
            note = f"  # {invite.note}" if invite.note else ""
            print(f"{invite.code}  {invite.used_count}/{invite.max_uses}  {status_text}{note}")
        return 0

    _dispatch(lambda: _run_account_action(run))


@invite_app.command("revoke")
def invite_revoke(code: Annotated[str, typer.Argument()]) -> None:
    """revoke a code"""
    from funread_api.accounts import accounts

    async def run(session) -> int:
        if not await accounts.revoke_invite(session, code):
            raise RuntimeError(f"没有这张邀请码：{code}")
        print(f"已吊销 {code}")
        return 0

    _dispatch(lambda: _run_account_action(run))


def _warn_deprecated_env() -> None:
    """机器上还设着已废弃的 ``FUNREAD_*`` 时喊一声，并说清该改到哪个键。

    不喊的话升级会**静默改变行为**：这些变量从 1.1.116 起一个都不读了，设过
    ``FUNREAD_DATABASE_URL`` 的机器会无声地掉到 funsecret 或本地 SQLite 上去。
    走 stderr 而不是日志 —— 人是在终端上敲这个命令的。
    """
    table = {**shared_config.DEPRECATED_ENV_VARS, **_DEPRECATED_ENV_VARS}
    for message in shared_config.describe_ignored_env(table):
        print(f"warning: {message}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    """Run the CLI over ``argv`` (``sys.argv[1:]`` when omitted) and return its exit code.

    ``[project.scripts]`` points here, so the return value *is* the process
    status. Typer always leaves through ``SystemExit`` -- including for
    ``--help`` and for a usage error, which keeps its status 2 -- so the code is
    unwrapped rather than propagated.
    """
    _warn_deprecated_env()
    try:
        app(args=argv, prog_name=CLI_NAME)
    except SystemExit as exit_signal:
        return int(exit_signal.code or 0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
