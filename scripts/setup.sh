#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
CLI="${FUNREAD_API_CLI:-funread-api}"
PACKAGE="funread-api"

usage() {
  cat >&2 <<'EOF'
Usage: scripts/setup.sh <action> [version]

Service: start | run | stop | restart | status
Install: install-dev | install-prod [version] | publish
Package: upgrade [version] | rollback <version> | uninstall
EOF
}

die() {
  printf 'error: %s\n' "$*" >&2
  exit 2
}

main() {
  local action="${1:-}"
  shift || true
  case "$action" in
    start|run|stop|restart|status)
      (( $# == 0 )) || die "$action accepts no arguments"
      exec "$CLI" server "$action"
      ;;
    install-dev)
      (( $# == 0 )) || die "install-dev accepts no arguments"
      cd "$ROOT"
      uv sync --extra dev
      uv build
      exec uv tool install --reinstall dist/*.whl
      ;;
    install-prod)
      (( $# <= 1 )) || die "install-prod accepts at most one version"
      if (( $# == 1 )); then
        exec uv tool install --reinstall "$PACKAGE==$1"
      fi
      exec uv tool install --reinstall "$PACKAGE"
      ;;
    publish)
      (( $# == 0 )) || die "publish accepts no arguments"
      cd "$ROOT"
      exec uv run funbuild build
      ;;
    upgrade)
      (( $# <= 1 )) || die "upgrade accepts at most one version"
      exec "$CLI" upgrade "$@"
      ;;
    rollback)
      (( $# == 1 )) || die "rollback requires a version"
      exec "$CLI" rollback "$1"
      ;;
    uninstall)
      (( $# == 0 )) || die "uninstall accepts no arguments"
      exec "$CLI" uninstall
      ;;
    -h|--help|help)
      usage
      ;;
    *)
      usage
      die "unknown action: ${action:-<empty>}"
      ;;
  esac
}

main "$@"
