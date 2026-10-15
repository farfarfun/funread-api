#!/usr/bin/env bash
#
# 把 funread-api 打成一个不依赖 Python 环境的单文件可执行程序。
#
# 用途：自托管的人下载一个文件就能跑，不用装 Python / uv，也不用担心共享环境里
# 的旧包盖掉新版（这个坑我们自己踩过好几次）。
#
#   ./scripts/build-binary.sh            # 产物在 dist/funread-api
#   ./scripts/build-binary.sh /tmp/out   # 指定输出目录
#
# 产物约 50 MB，冷启动约 1.1 秒（解包到临时目录的代价，--onefile 的固有开销）。
# 嫌慢可以把 --onefile 换成 --onedir：启动快得多，但变成一个目录而不是一个文件。

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${1:-$HERE/dist}"
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

echo "==> 在干净环境里构建（$WORK_DIR）"
uv venv --python 3.12 "$WORK_DIR/.venv" >/dev/null

#  **必须装在同一个环境里**：PyInstaller 只打包它能 import 到的东西，
#  `--hidden-import` 对没装的包无能为力。
uv pip install --python "$WORK_DIR/.venv/bin/python" --quiet pyinstaller
uv pip install --python "$WORK_DIR/.venv/bin/python" --quiet "$HERE"

#  asyncpg / psycopg2 **不是** funread-api 的声明依赖 —— 它们要编译扩展，而本地
#  开发只用 SQLite。但线上是 PostgreSQL，漏了的话二进制跑起来一切正常，直到第一
#  次连库才报 `ModuleNotFoundError: No module named 'asyncpg'`。这一行是踩过的坑。
uv pip install --python "$WORK_DIR/.venv/bin/python" --quiet asyncpg psycopg2-binary

cat > "$WORK_DIR/entry.py" <<'PY'
from funread_api.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
PY

echo "==> PyInstaller 打包"
#  `--collect-submodules` 而不是逐个 `--hidden-import`：这几个包都有运行时按名字
#  加载的子模块（uvicorn 的 loop/protocol 实现、funread 的源类型注册表），静态
#  分析扫不出来。
"$WORK_DIR/.venv/bin/pyinstaller" \
    --onefile \
    --name funread-api \
    --distpath "$OUT_DIR" \
    --workpath "$WORK_DIR/build" \
    --specpath "$WORK_DIR" \
    --collect-submodules funread \
    --collect-submodules funread_api \
    --collect-submodules funauth \
    --collect-submodules uvicorn \
    --hidden-import aiosqlite \
    --hidden-import asyncpg \
    --hidden-import psycopg2 \
    --noconfirm \
    "$WORK_DIR/entry.py"

echo
echo "==> 冒烟测试"
"$OUT_DIR/funread-api" --help >/dev/null
echo "    --help 正常"

#  真连一次库是唯一能证明 C 扩展都在的办法：lxml、psycopg2、asyncpg、bcrypt
#  任何一个没打进去，都只会在运行到那一步时才崩。
if "$OUT_DIR/funread-api" accounts list >/dev/null 2>&1; then
    echo "    连库正常（C 扩展齐全）"
else
    echo "    ⚠ 连不上库 —— 可能是本机没配库地址，也可能是驱动没打进去。"
    echo "      手动确认：$OUT_DIR/funread-api accounts list"
fi

echo
ls -lh "$OUT_DIR/funread-api" | awk '{print "==> 完成:", $NF, "(" $5 ")"}'
