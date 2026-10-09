#!/usr/bin/env bash
set -euo pipefail

# funread-api 是单服务仓库（service 类），所以不再分一层 dispatcher，
# 验证与生命周期边界都留在这个脚本里。

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

SERVICE_NAME="api"
CLI_NAME="funread-api"
PKG_NAME="funread-api"
PORT=18811
# 留空 = 用 CLI 自己的默认路径
# ${XDG_CONFIG_HOME:-~/.config}/farfarfun/funread-api/config.toml。
# 无论哪种，funread-api.pid 都由 CLI 自己写在实际生效的 config 同目录下，
# 这个脚本从不直接碰那个文件。
CONFIG_PATH=""
PYTHON="${PYTHON:-python3}"

readonly ROOT SERVICE_NAME CLI_NAME PKG_NAME PORT CONFIG_PATH PYTHON

usage() {
  printf 'Usage: %s <start|stop|restart|run|status|install-dev>\n' "${0##*/}" >&2
  printf '       %s <install-prod|upgrade> [version]\n' "${0##*/}" >&2
  printf '       %s rollback <version>\n' "${0##*/}" >&2
  printf '       %s uninstall\n' "${0##*/}" >&2
}

die() {
  printf 'error: %s\n' "$*" >&2
  exit 2
}

cli_args() {
  # 本地构建安装与索引固定版本安装暴露的是同一个 CLI，不要按 dev/prod 分支。
  CLI_ARGS=(--port "${PORT}")
  # 必须用 if，不能写成 `[[ -n ... ]] && CLI_ARGS+=(...)`：在 set -e 下，
  # 当 CONFIG_PATH 为空时后者的退出码就是 [[ ]] 自身的 1，而它是函数最后一条
  # 命令，这个 1 会成为 cli_args 的返回值并直接中止整个脚本。
  if [[ -n "${CONFIG_PATH}" ]]; then
    CLI_ARGS+=(--config "${CONFIG_PATH}")
  fi
}

require_cli() {
  command -v "${CLI_NAME}" >/dev/null 2>&1 ||
    die "找不到 ${CLI_NAME}，先执行：${0##*/} install-dev（或 install-prod）"
}

do_start() {
  require_cli
  cli_args
  "${CLI_NAME}" server start "${CLI_ARGS[@]}"
}

do_run() {
  require_cli
  cli_args
  exec "${CLI_NAME}" server run "${CLI_ARGS[@]}"
}

do_stop() {
  require_cli
  "${CLI_NAME}" server stop
}

do_restart() {
  do_stop
  do_start
}

do_status() {
  require_cli
  "${CLI_NAME}" server status
}

# 清掉上一次的构建产物与本地安装，从工作树重新构建并强制重装，
# 保证 ${CLI_NAME} 反映当前源码。
do_install_dev() {
  rm -rf dist build
  funbuild install
}

# 直接从索引装正式包。CLI 自己装不了自己，所以首装归这个脚本。
do_install_prod() {
  local version="${1:-}"
  "${PYTHON}" -m pip install "${PKG_NAME}${version:+==${version}}"
}

do_upgrade() {
  local version="${1:-}"
  if command -v "${CLI_NAME}" >/dev/null 2>&1; then
    "${CLI_NAME}" upgrade ${version:+"${version}"}
  else
    # CLI 还不存在时退回到包管理器，否则无从 upgrade。
    if [[ -n "${version}" ]]; then
      "${PYTHON}" -m pip install "${PKG_NAME}==${version}"
    else
      "${PYTHON}" -m pip install --upgrade "${PKG_NAME}"
    fi
  fi
}

do_rollback() {
  local version="$1"
  require_cli
  "${CLI_NAME}" rollback "${version}"
}

# 先停服务再卸载，不要卸一个还活着的安装。
do_uninstall() {
  do_stop || true
  require_cli
  "${CLI_NAME}" uninstall
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
    *)
      usage
      die "unknown action: ${action:-<empty>}"
      ;;
  esac
}

main "$@"
