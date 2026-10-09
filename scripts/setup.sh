#!/usr/bin/env bash
set -euo pipefail

# funread-api 是单服务仓库（service 类），所以不再分一层 dispatcher，
# 验证与生命周期边界都留在这个脚本里。
#
# 这个脚本只做转发：进程后台化、PID 文件、存活判定全由 funread-api CLI 自己负责
# （见 src/funread_api/cli.py），bash 这边不 nohup、不写 PID、不轮询。

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

SERVICE_NAME="api"
# 允许用 FUNREAD_API_CLI 指到别处，方便在没装进 PATH 的环境里直接试。
CLI_NAME="${FUNREAD_API_CLI:-funread-api}"
PKG_NAME="funread-api"
PORT=18811

readonly ROOT SERVICE_NAME CLI_NAME PKG_NAME PORT

usage() {
  printf 'Usage: %s <start|stop|restart|run|status|install-dev>\n' "${0##*/}" >&2
  printf '       %s <install-prod|upgrade> [version]\n' "${0##*/}" >&2
  printf '       %s rollback <version>\n' "${0##*/}" >&2
  printf '       %s uninstall\n' "${0##*/}" >&2
  printf '\n' >&2
  printf '构建发布不是本脚本的 action：由 dev 仓库根的 `funbuild build` 统一 fan out。\n' >&2
}

die() {
  printf 'error: %s\n' "$*" >&2
  exit 2
}

require_cli() {
  command -v "${CLI_NAME}" >/dev/null 2>&1 ||
    die "找不到 ${CLI_NAME}，先执行：${0##*/} install-dev（或 install-prod）"
}

require_uv() {
  command -v uv >/dev/null 2>&1 || die "找不到 uv，请先安装 uv"
}

do_start() {
  require_cli
  exec "${CLI_NAME}" server start --port "${PORT}"
}

do_run() {
  require_cli
  exec "${CLI_NAME}" server run --port "${PORT}"
}

do_restart() {
  require_cli
  exec "${CLI_NAME}" server restart --port "${PORT}"
}

do_stop() {
  require_cli
  exec "${CLI_NAME}" server stop
}

do_status() {
  require_cli
  exec "${CLI_NAME}" server status
}

# 从工作树重新构建并强制重装，保证 ${CLI_NAME} 反映当前源码。
# 装成 uv tool 而不是 pip install 进共享 site-packages 是故意的：
# 本机 ~/opt/py312/site-packages 里有非 editable 的旧 funread，装进去会被它遮挡。
do_install_dev() {
  require_uv
  rm -rf dist build
  uv sync --extra dev
  uv build
  exec uv tool install --reinstall dist/*.whl
}

# 直接从索引装正式包。CLI 自己装不了自己，所以首装归这个脚本。
do_install_prod() {
  local version="${1:-}"
  require_uv
  exec uv tool install --reinstall "${PKG_NAME}${version:+==${version}}"
}

do_upgrade() {
  local version="${1:-}"
  if command -v "${CLI_NAME}" >/dev/null 2>&1; then
    exec "${CLI_NAME}" upgrade ${version:+"${version}"}
  fi
  # CLI 还不存在时退回包管理器，否则无从 upgrade。
  require_uv
  exec uv tool install --reinstall "${PKG_NAME}${version:+==${version}}"
}

do_rollback() {
  local version="$1"
  require_cli
  exec "${CLI_NAME}" rollback "${version}"
}

# CLI 的 uninstall 自己会先停服务，这里不重复停一遍。
do_uninstall() {
  require_cli
  exec "${CLI_NAME}" uninstall
}

main() {
  local action="${1:-}"

  case "${action}" in
    start | stop | restart | run | status)
      (( $# == 1 )) || {
        usage
        die "${action} 不接受额外参数"
      }
      "do_${action}"
      ;;
    install-dev | uninstall)
      (( $# == 1 )) || {
        usage
        die "${action} 不接受额外参数"
      }
      "do_${action//-/_}"
      ;;
    install-prod | upgrade)
      (( $# <= 2 )) || {
        usage
        die "${action} 最多接受一个 version 参数"
      }
      "do_${action//-/_}" "${2:-}"
      ;;
    rollback)
      (( $# == 2 )) || {
        usage
        die "rollback 必须显式给出版本号"
      }
      do_rollback "$2"
      ;;
    publish | build)
      usage
      die "${action} 不是本脚本的 action：构建发布由 dev 仓库根的 funbuild build 统一处理"
      ;;
    -h | --help | help)
      usage
      ;;
    *)
      usage
      die "unknown action: ${action:-<empty>}"
      ;;
  esac
}

main "$@"
