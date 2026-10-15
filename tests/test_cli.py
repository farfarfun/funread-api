import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

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


def _await_healthy(port: int) -> None:
    import urllib.request

    for _ in range(50):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=0.2) as r:
                assert r.read() == b'{"status":"ok"}'
                return
        except OSError:
            time.sleep(0.1)
    raise AssertionError("server never became healthy")


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def test_state_lives_next_to_the_resolved_config(tmp_path):
    """PID and log files belong beside the config, not under the current directory."""
    config = tmp_path / "conf" / "funread-api.toml"
    config.parent.mkdir()
    config.write_text("", encoding="utf-8")
    port = _free_port()
    workdir = tmp_path / "elsewhere"
    workdir.mkdir()

    started = _run_cli(workdir, "server", "start", "--config", str(config), "--port", str(port))
    assert started.returncode == 0, started.stderr
    try:
        assert (config.parent / "funread-api.pid").is_file()
        assert (config.parent / "funread-api.log").is_file()
        # The old behaviour -- state under the working directory -- is gone.
        assert not (workdir / ".run").exists()
        _await_healthy(port)
    finally:
        stopped = _run_cli(workdir, "server", "stop", "--config", str(config))
    assert stopped.returncode == 0, stopped.stderr
    assert not (config.parent / "funread-api.pid").exists()


def test_stop_works_from_an_unrelated_directory(tmp_path):
    """The regression that motivated moving the PID file.

    State used to be written to ``Path.cwd()/.run``, so a stop issued from any
    other directory reported "not running" and left the process alive.
    """
    config = tmp_path / "config.toml"
    config.write_text("", encoding="utf-8")
    port = _free_port()
    start_dir = tmp_path / "start-here"
    stop_dir = tmp_path / "stop-from-here"
    start_dir.mkdir()
    stop_dir.mkdir()

    started = _run_cli(start_dir, "server", "start", "--config", str(config), "--port", str(port))
    assert started.returncode == 0, started.stderr
    _await_healthy(port)

    stopped = _run_cli(stop_dir, "server", "stop", "--config", str(config))
    assert stopped.returncode == 0, stopped.stderr
    assert "stopped" in stopped.stdout
    assert not (tmp_path / "funread-api.pid").exists()

    # And the service really is gone -- not merely forgotten about. (Checking the
    # port is bindable instead would race against TIME_WAIT.)
    import urllib.error
    import urllib.request

    with pytest.raises((OSError, urllib.error.URLError)):
        urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1)


def test_status_reports_version_and_config(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text("port = 19999\n", encoding="utf-8")
    result = _run_cli(tmp_path, "server", "status", "--config", str(config))
    assert result.returncode == 0, result.stderr
    assert "not running" in result.stdout
    assert str(config) in result.stdout


def test_start_refuses_a_second_instance_on_the_same_config(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text("", encoding="utf-8")
    port = _free_port()

    first = _run_cli(tmp_path, "server", "start", "--config", str(config), "--port", str(port))
    assert first.returncode == 0, first.stderr
    try:
        _await_healthy(port)
        second = _run_cli(tmp_path, "server", "start", "--config", str(config))
        assert second.returncode == 1
        assert "already running" in second.stderr
    finally:
        _run_cli(tmp_path, "server", "stop", "--config", str(config))


# ---------------------------------------------------------------------------
# Settings resolution
# ---------------------------------------------------------------------------


def _settings(monkeypatch, tmp_path, **flags) -> cli.Settings:
    # 必须隔离 HOME：默认路径是 ``~/.farfarfun/funread/api/``，不改 HOME 的话这些用例会
    # 读到开发机上真实的那份 config.toml（断言随机失败），而走默认路径还会触发一次迁移，
    # 把真实旧目录里的 session-secret 搬走。XDG_CONFIG_HOME 一起指到 tmp 下，是为了让
    # paths.legacy_state_dir() 也落在 tmp 里 —— 它按老办法算，仍然看这个变量。
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    # ``flags`` 就是 `server run` 那几个选项解析出来的值；选项本身由 Typer 解析，
    # 端到端那部分由上面的 subprocess 用例覆盖。
    return cli._resolve_settings(**flags)


def test_defaults_without_any_config(monkeypatch, tmp_path):
    monkeypatch.delenv("FUNREAD_API_HOST", raising=False)
    monkeypatch.delenv("FUNREAD_API_PORT", raising=False)
    settings = _settings(monkeypatch, tmp_path)
    assert settings.host == cli.DEFAULT_HOST
    assert settings.port == cli.DEFAULT_PORT
    assert (
        settings.config_path == tmp_path / "home" / ".farfarfun" / "funread" / "api" / "config.toml"
    )


def test_environment_beats_the_hardcoded_default(monkeypatch, tmp_path):
    monkeypatch.setenv("FUNREAD_API_HOST", "0.0.0.0")
    monkeypatch.setenv("FUNREAD_API_PORT", "20001")
    settings = _settings(monkeypatch, tmp_path)
    assert (settings.host, settings.port) == ("0.0.0.0", 20001)


@pytest.mark.parametrize(
    ("name", "body"),
    [
        ("config.toml", 'host = "10.0.0.1"\nport = 20002\n'),
        ("config.json", json.dumps({"host": "10.0.0.1", "port": 20002})),
        ("config.env", "FUNREAD_API_HOST=10.0.0.1\nFUNREAD_API_PORT=20002\n"),
    ],
)
def test_config_file_beats_the_environment(monkeypatch, tmp_path, name, body):
    monkeypatch.setenv("FUNREAD_API_HOST", "127.0.0.9")
    monkeypatch.setenv("FUNREAD_API_PORT", "20099")
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    settings = _settings(monkeypatch, tmp_path, config=str(path))
    assert (settings.host, settings.port) == ("10.0.0.1", 20002)


def test_flags_beat_the_config_file(monkeypatch, tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('host = "10.0.0.1"\nport = 20002\n', encoding="utf-8")
    settings = _settings(monkeypatch, tmp_path, config=str(path), host="::1", port=20003)
    assert (settings.host, settings.port) == ("::1", 20003)


def test_server_table_is_accepted(monkeypatch, tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[server]\nhost = "10.0.0.2"\nport = 20004\n', encoding="utf-8")
    settings = _settings(monkeypatch, tmp_path, config=str(path))
    assert (settings.host, settings.port) == ("10.0.0.2", 20004)


def test_env_file_comments_and_quotes(monkeypatch, tmp_path):
    path = tmp_path / "config.env"
    path.write_text('# comment\nFUNREAD_API_HOST="10.0.0.3"\n\nport=20005\n', encoding="utf-8")
    settings = _settings(monkeypatch, tmp_path, config=str(path))
    assert (settings.host, settings.port) == ("10.0.0.3", 20005)


def test_missing_default_config_is_not_an_error(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    assert not settings.config_path.exists()
    assert settings.port == cli.DEFAULT_PORT


def test_missing_explicit_config_is_an_error(monkeypatch, tmp_path):
    with pytest.raises(RuntimeError, match="config file not found"):
        _settings(monkeypatch, tmp_path, config=str(tmp_path / "absent.toml"))


def test_unsupported_config_extension_is_rejected(monkeypatch, tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("port: 1\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="unsupported config extension"):
        _settings(monkeypatch, tmp_path, config=str(path))


def test_malformed_config_is_rejected(monkeypatch, tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("port = = 1\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="cannot parse config file"):
        _settings(monkeypatch, tmp_path, config=str(path))


def test_non_numeric_port_is_rejected(monkeypatch, tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('port = "http"\n', encoding="utf-8")
    with pytest.raises(RuntimeError, match="invalid port"):
        _settings(monkeypatch, tmp_path, config=str(path))


# ---------------------------------------------------------------------------
# Contract and package actions
# ---------------------------------------------------------------------------


def _command_names(app) -> set[str]:
    """The names a Typer app answers to, one level down."""
    return {info.name for info in (*app.registered_commands, *app.registered_groups)}


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
    # Deliberately absent: a CLI cannot install itself before it exists. Check the
    # registered command names rather than the help text -- rollback's description
    # legitimately contains the word "install".
    assert "install" not in _command_names(cli.app)


@pytest.mark.parametrize(
    "arguments",
    [
        (),
        ("server",),
        ("accounts",),
        ("accounts", "invite"),
        ("bogus",),
        ("server", "bogus"),
        ("rollback",),
        ("server", "run", "--port", "not-a-number"),
    ],
)
def test_usage_errors_exit_two(tmp_path, arguments):
    """A group without a subcommand, an unknown name and a bad value are all usage errors.

    Status 2 for these and 1 for a refusal from the service itself (see
    ``test_start_refuses_a_second_instance_on_the_same_config``) is the contract
    `scripts/setup.sh` execs into; it is pinned here because it is a property of
    the CLI framework rather than of any one command.
    """
    assert _run_cli(tmp_path, *arguments).returncode == 2


def test_every_server_action_takes_config(tmp_path):
    for action in ("run", "start", "restart", "stop", "status"):
        result = subprocess.run(
            [sys.executable, "-m", "funread_api.cli", "server", action, "--help"],
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, action
        assert "--config" in result.stdout, action


def test_package_actions_use_uv_tools(monkeypatch, tmp_path):
    commands: list[list[str]] = []
    monkeypatch.setattr(cli.shutil, "which", lambda command: "/usr/bin/uv")
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda command, check: commands.append(command) or type("Result", (), {"returncode": 0})(),
    )
    monkeypatch.setattr(cli, "_stop_server", lambda settings: 0)
    settings = cli.Settings(config_path=tmp_path / "config.toml", host="127.0.0.1", port=1)

    assert cli._upgrade(None) == 0
    assert cli._rollback("0.1.1") == 0
    assert cli._uninstall(settings) == 0
    assert commands == [
        ["/usr/bin/uv", "tool", "install", "--reinstall", "--upgrade", "funread-api"],
        ["/usr/bin/uv", "tool", "install", "--reinstall", "funread-api==0.1.1"],
        ["/usr/bin/uv", "tool", "uninstall", "funread-api"],
    ]


def test_stop_falls_back_to_funshell_when_the_pid_file_is_gone(monkeypatch, tmp_path):
    """Covers state left behind by the old cwd-relative layout."""
    settings = cli.Settings(config_path=tmp_path / "config.toml", host="127.0.0.1", port=20006)
    calls: list[int] = []
    monkeypatch.setattr(cli, "_kill_by_port", lambda port: calls.append(port) or [4242])

    assert cli._stop_server(settings) == 0
    assert calls == [20006]
