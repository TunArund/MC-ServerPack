#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
  echo "错误：需要 Docker Engine 与 Docker Compose 插件" >&2
  exit 127
fi

case "${1:-help}" in
  up)
    exec docker compose up -d --build
    ;;
  down)
    exec docker compose down
    ;;
  restart)
    exec docker compose restart
    ;;
  status)
    exec docker compose ps
    ;;
  logs)
    shift
    exec docker compose logs "$@"
    ;;
  get-user)
    if [[ -n "$(docker compose ps --status running --quiet monitor)" ]]; then
      echo "错误：请先执行 ./monitor.sh down，避免重复连接 QQ Gateway" >&2
      exit 1
    fi
    exec docker compose run --rm --no-deps --build monitor python -m src.app get-user
    ;;
  help|-h|--help)
    echo "用法：./monitor.sh up|down|restart|status|get-user|logs [docker compose logs 参数]"
    ;;
  *)
    echo "错误：未知命令：$1" >&2
    exit 2
    ;;
esac
