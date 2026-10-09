"""Command-line entry point for the funread API service.

Settings resolution, highest priority first:

1. an explicit CLI flag (``--host`` / ``--port``)
2. the config file (``--config``, else the default path below)
3. the ``FUNREAD_API_HOST`` / ``FUNREAD_API_PORT`` environment variables
4. the hardcoded loopback defaults

The config file defaults to
``${XDG_CONFIG_HOME:-~/.config}/farfarfun/funread-api/config.toml`` and is
parsed by extension (``.toml`` / ``.json`` / ``.env``). Runtime state -- the
PID and log files -- lives *next to whichever config actually resolved*, not
in a fixed directory and not relative to the current directory: a host running
two instances off two configs keeps two independent sets of state, and
``stop`` works from anywhere.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

PACKAGE_NAME = "funread-api"
CLI_NAME = "funread-api"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18811
STOP_TIMEOUT_SECONDS = 10
START_TIMEOUT_SECONDS = 5

#: Config keys we understand, after normalisation. A ``.env`` file naming them
#: ``FUNREAD_API_PORT`` and a ``.toml`` file naming them ``port`` both land here.
_KNOWN_KEYS = ("host", "port")


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
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "farfarfun" / CLI_NAME / "config.toml"


def _parse_env_file(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def _normalise_keys(raw: dict) -> dict[str, object]:
    """Fold ``FUNREAD_API_PORT`` / ``PORT`` / ``port`` onto the same key."""
    normalised: dict[str, object] = {}
    for key, value in raw.items():
        name = str(key).strip().lower().removeprefix("funread_api_")
        if name in _KNOWN_KEYS:
            normalised[name] = value
    return normalised


def _read_config(path: Path, *, explicit: bool) -> dict[str, object]:
    """Read and normalise a config file.

    A missing file is only an error when the user named it: the *default* path
    not existing is the normal case on a fresh install.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        if explicit:
            raise RuntimeError(f"config file not found: {path}") from None
        return {}
    except OSError as error:
        raise RuntimeError(f"cannot read config file {path}: {error}") from None

    suffix = path.suffix.lower()
    try:
        if suffix == ".toml":
            raw = tomllib.loads(text)
        elif suffix == ".json":
            raw = json.loads(text)
        elif suffix == ".env":
            raw = _parse_env_file(text)
        else:
            raise RuntimeError(
                f"unsupported config extension {suffix or '(none)'}: expected .toml, .json or .env"
            )
    except (tomllib.TOMLDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"cannot parse config file {path}: {error}") from None

    if not isinstance(raw, dict):
        raise RuntimeError(f"config file {path} must contain a mapping at the top level")
    # A [server] table is accepted so a shared config can grow other sections.
    section = raw.get("server")
    if isinstance(section, dict):
        raw = {**raw, **section}
    return _normalise_keys(raw)


def _coerce_port(value: object, *, source: str) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise RuntimeError(f"invalid port from {source}: {value!r}") from None


def _resolve_settings(arguments: argparse.Namespace) -> Settings:
    explicit = getattr(arguments, "config", None)
    config_path = Path(explicit).expanduser() if explicit else _default_config_path()
    config = _read_config(config_path, explicit=explicit is not None)

    host = getattr(arguments, "host", None)
    if host is None:
        host = config.get("host") or os.environ.get("FUNREAD_API_HOST") or DEFAULT_HOST

    port_value = getattr(arguments, "port", None)
    if port_value is not None:
        port = port_value
    elif "port" in config:
        port = _coerce_port(config["port"], source=f"config file {config_path}")
    elif os.environ.get("FUNREAD_API_PORT"):
        port = _coerce_port(os.environ["FUNREAD_API_PORT"], source="FUNREAD_API_PORT")
    else:
        port = DEFAULT_PORT

    return Settings(config_path=config_path, host=str(host), port=port)


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
# Argument parsing
# ---------------------------------------------------------------------------


def _server_action(arguments: argparse.Namespace) -> int:
    settings = _resolve_settings(arguments)
    if arguments.action == "run":
        return _run_server(settings)
    if arguments.action == "start":
        return _start_server(settings)
    if arguments.action == "stop":
        return _stop_server(settings)
    if arguments.action == "restart":
        _stop_server(settings)
        return _start_server(settings)
    return _status_server(settings)


def _add_config_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        metavar="PATH",
        help=(
            "config file (.toml/.json/.env); defaults to "
            "${XDG_CONFIG_HOME:-~/.config}/farfarfun/funread-api/config.toml. "
            "The PID and log files live in the same directory."
        ),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=CLI_NAME, description="Manage the funread API service")
    subcommands = parser.add_subparsers(dest="command", required=True)

    server = subcommands.add_parser("server", help="manage the API service")
    server_actions = server.add_subparsers(dest="action", required=True)
    for action in ("run", "start", "restart"):
        command = server_actions.add_parser(action)
        _add_config_flag(command)
        # Default None, not DEFAULT_HOST/PORT: _resolve_settings has to be able
        # to tell "the user passed this" from "fall through to the config file".
        command.add_argument("--host", default=None)
        command.add_argument("--port", type=int, default=None)
    # stop/status need --config and --port too: the former locates the PID file,
    # the latter is the fallback when that file is gone.
    for action in ("stop", "status"):
        command = server_actions.add_parser(action)
        _add_config_flag(command)
        command.add_argument("--port", type=int, default=None)

    upgrade = subcommands.add_parser("upgrade", help="upgrade to the latest or a named version")
    upgrade.add_argument("version", nargs="?")
    rollback = subcommands.add_parser("rollback", help="install a specific earlier version")
    rollback.add_argument("version")
    uninstall = subcommands.add_parser(
        "uninstall", help="stop the service and uninstall the package"
    )
    _add_config_flag(uninstall)
    uninstall.add_argument("--port", type=int, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "server":
            return _server_action(arguments)
        if arguments.command == "upgrade":
            return _upgrade(arguments.version)
        if arguments.command == "rollback":
            return _rollback(arguments.version)
        return _uninstall(_resolve_settings(arguments))
    except RuntimeError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
