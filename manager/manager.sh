#!/usr/bin/env bash
set -Eeuo pipefail

if [[ -n "${BASH_SOURCE[0]:-}" ]]; then
  ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
else
  ROOT_DIR="$PWD"
fi

check_requirements() {
  local failed=0
  if ! command -v python3 >/dev/null 2>&1 ||
     ! python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 9))'; then
    echo "错误：需要 Python 3.9 或更高版本" >&2
    failed=1
  fi
  if ! python3 -c 'import venv, ensurepip' >/dev/null 2>&1; then
    echo "错误：需要 Python venv 与 ensurepip；Debian/Ubuntu 请安装 python3-venv" >&2
    failed=1
  fi
  if ! command -v docker >/dev/null 2>&1 ||
     ! docker compose version >/dev/null 2>&1 ||
     ! docker info >/dev/null 2>&1; then
    echo "错误：需要可用的 Docker Engine 与 Docker Compose 插件" >&2
    failed=1
  fi
  if ! command -v crontab >/dev/null 2>&1; then
    echo "错误：需要 crontab" >&2
    failed=1
  fi
  return "$failed"
}

case "${1:-}" in
  check)
    check_requirements
    echo "Manager 运行环境有效"
    exit 0
    ;;
  install)
    if [[ "${2:-}" == "docker" ]]; then
      if (( $# != 2 )); then
        echo "用法：./manager.sh install docker" >&2
        exit 2
      fi
      cd "$ROOT_DIR"
      exec python3 -m lib.cli install docker
    fi
    if (( $# != 1 )); then
      echo "用法：./manager.sh install [docker]" >&2
      exit 2
    fi
    check_requirements
    cd "$ROOT_DIR"
    python3 -m venv .venv
    ./.venv/bin/pip install -r lib/requirements.txt
    ./.venv/bin/python -m lib.cli schedule install
    echo "Manager 安装完成"
    exit 0
    ;;
  apply-config)
    check_requirements
    cd "$ROOT_DIR"
    PYTHON="$ROOT_DIR/.venv/bin/python"
    if [[ ! -x "$PYTHON" ]]; then
      PYTHON=python3
    fi
    "$PYTHON" -c 'from lib.config import load_limits; load_limits()'
    "$PYTHON" -m lib.cli schedule install
    echo "Manager 配置已应用"
    exit 0
    ;;
esac

if ! command -v python3 >/dev/null 2>&1; then
  echo "错误：需要 Python 3.9 或更高版本" >&2
  exit 127
fi
if ! python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 9))'; then
  echo "错误：需要 Python 3.9 或更高版本" >&2
  exit 1
fi

cd "$ROOT_DIR"
PYTHON="$ROOT_DIR/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  PYTHON=python3
fi
exec "$PYTHON" -m lib.cli "$@"
