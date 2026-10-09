"""Command-line entry point for the funread API service."""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import time
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

PACKAGE_NAME = "funread-api"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18811
STOP_TIMEOUT_SECONDS = 10


def _runtime_dir() -> Path:
    return Path.cwd() / ".run"


def _state_paths() -> tuple[Path, Path]:
    runtime_dir = _runtime_dir()
    return runtime_dir / "funread-api.pid", runtime_dir / "funread-api.log"


def _read_pid() -> int | None:
    pid_file, _ = _state_paths()
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
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


def _clear_state() -> None:
    _state_paths()[0].unlink(missing_ok=True)


def _installed_version() -> str:
    try:
        return version(PACKAGE_NAME)
    except PackageNotFoundError:
        return "unknown"


def _run_server(host: str, port: int) -> int:
    existing = _read_pid()
    if existing is not None and existing != os.getpid() and _pid_is_live(existing):
        raise RuntimeError(f"funread-api is already running (pid {existing})")

    runtime_dir = _runtime_dir()
    runtime_dir.mkdir(mode=0o700, exist_ok=True)
    pid_file, _ = _state_paths()
    pid_file.write_text(f"{os.getpid()}\n", encoding="utf-8")
    try:
        import uvicorn

        uvicorn.run("funread_api.app:create_app", factory=True, host=host, port=port)
    finally:
        _clear_state()
    return 0


def _start_server(host: str, port: int) -> int:
    existing = _read_pid()
    if existing is not None and _pid_is_live(existing):
        raise RuntimeError(f"funread-api is already running (pid {existing})")
    _clear_state()

    runtime_dir = _runtime_dir()
    runtime_dir.mkdir(mode=0o700, exist_ok=True)
    _, log_file = _state_paths()
    command = [
        sys.executable,
        "-m",
        "funread_api.cli",
        "server",
        "run",
        "--host",
        host,
        "--port",
        str(port),
    ]
    with log_file.open("ab") as log:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        pid = _read_pid()
        if process.poll() is not None:
            break
        if pid is not None and _pid_is_live(pid):
            print(f"funread-api {_installed_version()} started (pid {pid}, port {port})")
            return 0
        time.sleep(0.1)
    _clear_state()
    raise RuntimeError(f"funread-api failed to start; see {log_file}")


def _stop_server() -> int:
    pid = _read_pid()
    if pid is None or not _pid_is_live(pid):
        _clear_state()
        print("funread-api is not running")
        return 0
    if not _pid_belongs_to_service(pid):
        raise RuntimeError(f"PID {pid} is not a funread-api service; refusing to stop it")
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + STOP_TIMEOUT_SECONDS
    while _pid_is_live(pid):
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"funread-api did not stop within {STOP_TIMEOUT_SECONDS}s (pid {pid})"
            )
        time.sleep(0.1)
    _clear_state()
    print("funread-api stopped")
    return 0


def _status_server() -> int:
    pid = _read_pid()
    if pid is not None and _pid_is_live(pid):
        print(f"funread-api {_installed_version()}: running (pid {pid})")
    else:
        if pid is not None:
            _clear_state()
        print(f"funread-api {_installed_version()}: not running")
    return 0


def _run_uv(arguments: list[str]) -> int:
    executable = shutil.which("uv")
    if executable is None:
        raise RuntimeError("uv is required to manage an installed funread-api package")
    return subprocess.run([executable, *arguments], check=False).returncode


def _upgrade(version_name: str | None) -> int:
    target = f"{PACKAGE_NAME}=={version_name}" if version_name else PACKAGE_NAME
    return _run_uv(["tool", "install", "--reinstall", "--upgrade", target])


def _rollback(version_name: str) -> int:
    return _run_uv(["tool", "install", "--reinstall", f"{PACKAGE_NAME}=={version_name}"])


def _uninstall() -> int:
    _stop_server()
    return _run_uv(["tool", "uninstall", PACKAGE_NAME])


def _server_action(arguments: argparse.Namespace) -> int:
    if arguments.action == "run":
        return _run_server(arguments.host, arguments.port)
    if arguments.action == "start":
        return _start_server(arguments.host, arguments.port)
    if arguments.action == "stop":
        return _stop_server()
    if arguments.action == "restart":
        _stop_server()
        return _start_server(arguments.host, arguments.port)
    return _status_server()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="funread-api", description="Manage the funread API service"
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    server = subcommands.add_parser("server", help="manage the API service")
    server_actions = server.add_subparsers(dest="action", required=True)
    for action in ("run", "start", "restart"):
        command = server_actions.add_parser(action)
        command.add_argument("--host", default=DEFAULT_HOST)
        command.add_argument("--port", type=int, default=DEFAULT_PORT)
    server_actions.add_parser("stop")
    server_actions.add_parser("status")

    upgrade = subcommands.add_parser("upgrade", help="upgrade to the latest or a named version")
    upgrade.add_argument("version", nargs="?")
    rollback = subcommands.add_parser("rollback", help="install a specific earlier version")
    rollback.add_argument("version")
    subcommands.add_parser("uninstall", help="stop the service and uninstall the package")
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
        return _uninstall()
    except RuntimeError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
