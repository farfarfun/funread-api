"""配置目录解析与 1.1.114 的一次性旧路径迁移。

这些用例都把 ``HOME`` 与 ``XDG_CONFIG_HOME`` 指到 tmp 下：被测对象就是「家目录里的某个
固定位置」，不隔离的话迁移会去动开发机上真实的 ``session-secret``。
"""

from __future__ import annotations

import pytest

from funread_api import paths


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    return tmp_path


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_state_dir_is_under_home_and_ignores_xdg(home):
    assert paths.state_dir() == home / "home" / ".farfarfun" / "funread" / "api"
    assert paths.default_config_path() == paths.state_dir() / "config.toml"
    # 旧位置反过来必须认 XDG_CONFIG_HOME，否则设过它的机器上找不到要迁移的文件。
    assert paths.legacy_state_dir() == home / "xdg" / "farfarfun" / "funread-api"


def test_migration_moves_config_and_secret_but_leaves_pid_and_log(home):
    legacy = paths.legacy_state_dir()
    _write(legacy / "config.toml", "[server]\nport = 19999\n")
    _write(legacy / "session-secret", "s3cret")
    _write(legacy / "funread-api.pid", "4242")
    _write(legacy / "funread-api.log", "old log\n")

    assert paths.migrate_legacy_state() == ["config.toml", "session-secret"]

    assert (paths.state_dir() / "config.toml").read_text(encoding="utf-8") == (
        "[server]\nport = 19999\n"
    )
    assert (paths.state_dir() / "session-secret").read_text(encoding="utf-8") == "s3cret"
    # PID 搬过来会让新位置的 stop 去对一个与新进程无关的 PID；日志同理，重启自然重建。
    assert (legacy / "funread-api.pid").is_file()
    assert (legacy / "funread-api.log").is_file()
    assert not (paths.state_dir() / "funread-api.pid").exists()


def test_migration_is_idempotent(home):
    _write(paths.legacy_state_dir() / "config.toml", "")
    assert paths.migrate_legacy_state() == ["config.toml"]
    assert paths.migrate_legacy_state() == []


def test_migration_is_a_noop_without_a_legacy_dir(home):
    assert paths.migrate_legacy_state() == []


def test_migration_never_clobbers_and_is_decided_per_file(home):
    """新位置已有 config.toml 时，``session-secret`` 仍要搬。

    这是真实场景：fundeploy 在服务起不来时会主动在新位置补一份 config.toml。若按
    「新目录有 config.toml 就整体跳过」，那份补出来的文件会把迁移永久挡住，
    ``session-secret`` 留在旧目录 —— 现象是升级之后所有人被登出。
    """
    _write(paths.legacy_state_dir() / "config.toml", "legacy = true\n")
    _write(paths.legacy_state_dir() / "session-secret", "s3cret")
    _write(paths.default_config_path(), "deployed = true\n")

    assert paths.migrate_legacy_state() == ["session-secret"]

    assert paths.default_config_path().read_text(encoding="utf-8") == "deployed = true\n"
    assert (paths.state_dir() / "session-secret").read_text(encoding="utf-8") == "s3cret"
    # 没搬走的那份留在原处，不删 —— 迁移只负责搬，不负责清理。
    assert (paths.legacy_state_dir() / "config.toml").is_file()
