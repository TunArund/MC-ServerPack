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
  add-target)
    if (( $# > 2 )); then
      echo "用法：./monitor.sh add-target [四位验证码]" >&2
      exit 2
    fi
    was_running=false
    if [[ -n "$(docker compose ps --status running --quiet monitor)" ]]; then
      docker compose stop monitor >/dev/null
      was_running=true
    fi
    restore_monitor() {
      if [[ "$was_running" == true ]]; then
        docker compose up -d monitor >/dev/null
      fi
    }
    trap restore_monitor EXIT
    command=(python -m src.app add-target)
    if [[ -n "${2:-}" ]]; then
      command+=("$2")
    fi
    docker compose run --rm --no-deps --build monitor "${command[@]}"
    ;;
  help|-h|--help)
    echo "用法：./monitor.sh up|down|restart|status|add-target|logs [docker compose logs 参数]"
    ;;
  *)
    echo "错误：未知命令：$1" >&2
    exit 2
    ;;
esac
