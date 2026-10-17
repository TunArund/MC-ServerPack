#!/usr/bin/env bash
set -Eeuo pipefail

if [[ -n "${BASH_SOURCE[0]:-}" ]]; then
  ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
else
  ROOT_DIR="$PWD"
fi
cd "$ROOT_DIR"

check_requirements() {
  if ! command -v docker >/dev/null 2>&1 ||
     ! docker compose version >/dev/null 2>&1 ||
     ! docker info >/dev/null 2>&1; then
    echo "错误：需要可用的 Docker Engine 与 Docker Compose 插件" >&2
    return 1
  fi
}

case "${1:-help}" in
  check)
    check_requirements
    echo "Monitor 运行环境有效"
    ;;
  install)
    check_requirements
    exec docker compose up -d --build
    ;;
  apply-config)
    check_requirements
    docker compose run --rm --no-deps monitor \
      python -c 'from src.config import load; load()'
    exec docker compose restart monitor
    ;;
  start)
    check_requirements
    exec docker compose start monitor
    ;;
  stop)
    check_requirements
    exec docker compose stop monitor
    ;;
  restart)
    check_requirements
    exec docker compose restart
    ;;
  status)
    check_requirements
    exec docker compose ps
    ;;
  logs)
    check_requirements
    shift
    exec docker compose logs "$@"
    ;;
  add-target)
    check_requirements
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
    echo "用法：./monitor.sh check|install|apply-config|start|stop|restart|status|add-target|logs"
    ;;
  *)
    echo "错误：未知命令：$1" >&2
    exit 2
    ;;
esac
