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
  help|-h|--help)
    echo "用法：./monitor.sh up|down|restart|status|logs [docker compose logs 参数]"
    ;;
  *)
    echo "错误：未知命令：$1" >&2
    exit 2
    ;;
esac
