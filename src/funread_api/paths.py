"""配置目录的唯一真源，外加一次性的旧路径迁移。

1.1.114 之前这个路径在 ``cli.py`` 与 ``accounts.py`` 里各算了一遍（都是
``${XDG_CONFIG_HOME:-~/.config}/farfarfun/funread-api/``）。两份实现算同一个路径，
改布局时漏掉一处就会把 ``session-secret`` 留在旧目录里，效果是「升级之后所有人被
登出」—— 一个很难从现象倒推到原因的故障。所以集中到这个模块，两边都 import。
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

#: 组织级根目录。**故意不走 XDG_CONFIG_HOME**：这个目录里同时放 config.toml、PID、
#: 日志和 session-secret，后三样都不是 XDG 意义上的「配置」。要守 XDG 就得把它们拆到
#: config / state / runtime 三个目录里，排查问题时得翻三处；与其如此，不如像 ~/.ssh、
#: ~/.aws 那样在家目录下单独占一个根，一个服务的全部状态都在一处。
ORG_DIR = ".farfarfun"
PRODUCT_DIR = "funread"
COMPONENT_DIR = "api"

CONFIG_FILENAME = "config.toml"

#: 迁移时搬这两个。PID 与日志**不搬**：它们描述的是旧布局下起的那个进程，搬过来只会
#: 让新位置的 stop 去对一个已经和新进程无关的 PID。重启时自然重建。
MIGRATED_FILENAMES = (CONFIG_FILENAME, "session-secret")


def state_dir() -> Path:
    """``~/.farfarfun/funread/api`` —— config、PID、日志、session-secret 都在这。"""
    return Path.home() / ORG_DIR / PRODUCT_DIR / COMPONENT_DIR


def legacy_state_dir() -> Path:
    """1.1.114 之前的位置。

    必须按**老办法**算（含 XDG_CONFIG_HOME 这一层），否则在设过
    ``XDG_CONFIG_HOME`` 的机器上找不到要迁移的旧文件。
    """
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "farfarfun" / "funread-api"


def default_config_path() -> Path:
    return state_dir() / CONFIG_FILENAME


def migrate_legacy_state() -> list[str]:
    """把旧位置残留的 config.toml / session-secret 搬到新位置，返回搬过来的文件名。

    **逐个文件判断，不是「新目录有 config.toml 就整体跳过」**。差别在于部署工具
    （fundeploy）会在服务起不来时主动补一份 config.toml：如果按整体跳过，那份新建的
    config.toml 会把迁移挡掉，``session-secret`` 永远留在旧目录 —— 正好触发这个模块
    开头说的那个故障。按文件判断就没这个窗口。

    一次性由 ``shutil.move`` 本身保证：搬过之后源文件就不在了，之后每次调用都是空操作。

    迁移失败（权限、跨设备、目录建不出来）不抛异常：这是尽力而为的收尾动作，不该让
    服务起不来。调用方按「新位置没有这个文件」继续走即可。
    """
    target = state_dir()
    source = legacy_state_dir()
    if source == target or not source.is_dir():
        return []

    moved: list[str] = []
    for name in MIGRATED_FILENAMES:
        src = source / name
        # 目标已存在就别覆盖 —— 新位置的那份才是当前在用的。
        if not src.is_file() or (target / name).exists():
            continue
        try:
            target.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.move(str(src), str(target / name))
        except OSError:
            continue
        moved.append(name)
    return moved
