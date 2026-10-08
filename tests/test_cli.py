import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from funread_api import cli


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _run_cli(workdir: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ | {"FUNREAD_DATABASE_URL": f"sqlite:///{workdir / 'api.db'}"}
    return subprocess.run(
        [sys.executable, "-m", "funread_api.cli", *arguments],
        cwd=workdir,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def test_server_lifecycle_uses_one_state_file(tmp_path):
    port = _free_port()
    started = _run_cli(tmp_path, "server", "start", "--port", str(port))
    assert started.returncode == 0, started.stderr
    assert (tmp_path / ".run" / "funread-api.pid").is_file()
    try:
        for _ in range(50):
            try:
                import urllib.request

                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/healthz", timeout=0.2
                ) as response:
                    assert response.read() == b'{"status":"ok"}'
                    break
            except OSError:
                time.sleep(0.1)
        else:
            raise AssertionError("server never became healthy")

        status = _run_cli(tmp_path, "server", "status")
        assert status.returncode == 0
        assert "running" in status.stdout
    finally:
        stopped = _run_cli(tmp_path, "server", "stop")
    assert stopped.returncode == 0, stopped.stderr
    assert not (tmp_path / ".run" / "funread-api.pid").exists()


def test_cli_contract_is_exposed():
    result = subprocess.run(
        [sys.executable, "-m", "funread_api.cli", "--help"],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    assert all(
        command in result.stdout for command in ("server", "upgrade", "rollback", "uninstall")
    )


def test_package_actions_use_uv_tools(monkeypatch):
    commands: list[list[str]] = []
    monkeypatch.setattr(cli.shutil, "which", lambda command: "/usr/bin/uv")
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda command, check: commands.append(command) or type("Result", (), {"returncode": 0})(),
    )
    monkeypatch.setattr(cli, "_stop_server", lambda: 0)

    assert cli._upgrade(None) == 0
    assert cli._rollback("0.1.1") == 0
    assert cli._uninstall() == 0
    assert commands == [
        ["/usr/bin/uv", "tool", "install", "--reinstall", "--upgrade", "funread-api"],
        ["/usr/bin/uv", "tool", "install", "--reinstall", "funread-api==0.1.1"],
        ["/usr/bin/uv", "tool", "uninstall", "funread-api"],
    ]
