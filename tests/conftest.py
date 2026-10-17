import asyncio

import pytest

from funread.base import config
from funread.legado.reader import storage
from funread_api import accounts as accounts_module
from funread_api.v1.deps import (
    get_download_tracker,
    get_update_check_tracker,
    reset_reader_services,
)


@pytest.fixture(autouse=True)
def _no_colour_in_cli_output(monkeypatch):
    """关掉 rich 的彩色渲染，让断言 CLI 输出的用例在哪儿跑都一样。

    Typer 的 help 由 rich 渲染，而**上色会把选项名切成两段**：`--config` 实际写
    出来的是 ``ESC[1;36m-`` + ``-config`` + ``ESC[0m``，两截之间夹着转义序列。
    于是 ``"--config" in result.stdout`` 直接为假 —— 字符串确实不在里面。

    这件事只在终端被判定为支持颜色时发生。本地 pytest 下子进程的 stdout 是管道，
    rich 认定非 TTY、不上色，所以一直是绿的；**GitHub Actions 上 rich 会认为可以
    上色**，于是 `test_every_server_action_takes_config` 成了第一条
    「本地绿、CI 红」的用例（2026-10-17 这个仓库刚加上 CI 就撞上了）。

    设在 conftest 而不是各个用例里：三处跑 CLI 的地方（``_run_cli`` 和两个裸
    ``subprocess.run``）都继承父进程的环境变量，一处设置全部覆盖，以后新加的也
    自动带上。``NO_COLOR`` 是跨工具的事实标准（no-color.org），rich 认它。
    """
    monkeypatch.setenv("NO_COLOR", "1")


@pytest.fixture(autouse=True)
def _no_ambient_config(tmp_path, monkeypatch):
    """Cut the tests off from the machine's own configuration.

    Two ambient sources would otherwise leak in:

    * **funsecret** —— 真实的开发者密钥库，本机那份里是生产 PostgreSQL 地址和一个
      真的管理端口令。留着它的话"没配口令"就变成了"这台笔记本上没配口令"，而且本机
      设过的口令会把一堆无关用例变成 401。
    * **config.toml** —— `~/.farfarfun/funread/config.toml` 和上一个用例注册进去的
      那些文件。`shared_config_path()` 跟着 HOME 走，所以改 HOME 就够了；注册列表是
      进程内的全局状态，必须显式清掉。

    口令、`reader_public`、`register_open` 全部不设，让各层兜底值生效（注册默认
    **开**，闸门是邀请码行而不是一个静态口令）。想测鉴权的用例自己
    `config.set_overrides(api_password=...)`。
    """
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir(exist_ok=True)
    #  整条 funsecret 层断掉，不是只断某一个键：这个进程里 `resolve_setting` 的每一次
    #  调用都会走到它，一个一个 override 早晚漏一个。想测这一层的用例自己 setattr 回去。
    monkeypatch.setattr(config, "read_secret_value", lambda **_kwargs: None)
    config.reset_config_files()
    #  固定一个会话签名密钥：真的解析器会去读 funsecret，或者往配置目录里**写**一个
    #  密钥文件。
    config.set_overrides(session_secret="test-session-secret")
    yield
    config.clear_overrides()
    config.reset_config_files()


@pytest.fixture(autouse=True)
def _isolate_database(tmp_path, monkeypatch):
    """Point every test at its own SQLite file and cache root.

    Belt and braces over funread's own autouse fixture: this process resolves
    the database through ``funread.base.config``, which falls back to funsecret
    -- and on this machine that is the production PostgreSQL. A test that
    forgets to isolate must not reach it.

    用**进程内 override**，不是 `monkeypatch.setenv`：`FUNREAD_*` 那一层已经整个删
    掉了，继续 setenv 的话这个 fixture 会变成空操作，然后一路掉到生产库上。
    `apps/funread/tests/test_config.py::test_environment_variables_are_ignored`
    钉住了这件事。

    ``_INITIALIZED_DATABASES`` and the memoised services are keyed by URL /
    config revision, so both are cleared: otherwise the first test's engine (and
    its schema migration state) would be reused by every later one.
    """
    config.set_overrides(
        database_url=f"sqlite:///{tmp_path / 'funread-test.db'}",
        cache_root=str(tmp_path / "hubs"),
    )
    monkeypatch.setattr(storage, "_INITIALIZED_DATABASES", set())
    reset_reader_services()
    get_download_tracker().clear()
    get_update_check_tracker().clear()
    yield
    reset_reader_services()
    get_download_tracker().clear()
    get_update_check_tracker().clear()
    #  The account engine is async and cached by URL like the others, but it
    #  also has to be *disposed*: aiosqlite puts every connection on its own
    #  thread, and 600-odd tests each leaking one ends the run with
    #  "cannot schedule new futures after shutdown".
    asyncio.run(accounts_module.reset_async_engines())
