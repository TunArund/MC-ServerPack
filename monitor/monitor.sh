#!/usr/bin/env bash
set -Eeuo pipefail

if [[ -n "${BASH_SOURCE[0]:-}" ]]; then
  ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
else
  ROOT_DIR="$PWD"
fi
cd "$ROOT_DIR"

if [[ "$(id -u)" -eq 0 ]]; then
  echo "错误：请用普通用户运行 ./monitor.sh，不要使用 root" >&2
  exit 2
fi

# Manager 与 Monitor 统一用 MC_CONTAINER_UID/GID 映射普通用户。
export MC_CONTAINER_UID="$(id -u)"
export MC_CONTAINER_GID="$(id -g)"

check_requirements() {
  if ! command -v docker >/dev/null 2>&1 ||
     ! docker compose version >/dev/null 2>&1 ||
     ! docker info >/dev/null 2>&1; then
    echo "错误：需要可用的 Docker Engine 与 Docker Compose 插件" >&2
    return 1
  fi
}

compose_image() {
  docker compose config --images 2>/dev/null | head -n 1
}

source_hash() {
  {
    sha256sum Dockerfile .dockerignore 2>/dev/null || true
    find src -type f ! -path '*/__pycache__/*' ! -name '*.pyc' \
      ! -path 'src/message.yml' -print0 |
      sort -z | xargs -0 -r sha256sum
  } | sha256sum | cut -d' ' -f1
}

image_current() {
  local image current saved
  image="$(compose_image)"
  [[ -n "$image" ]] || return 1
  docker image inspect "$image" >/dev/null 2>&1 || return 1
  current="$(source_hash)"
  saved="$(cat data/image-source-hash 2>/dev/null || true)"
  [[ -n "$current" && "$current" == "$saved" ]]
}

ensure_manager_secret() {
  if [[ ! -f .env ]] || ! grep -Eq '^MANAGER_API_KEY=.{32,}$' .env; then
    echo "错误：.env 缺少 MANAGER_API_KEY（至少 32 个字符）" >&2
    echo "通过 Terminal 部署 Monitor 会自动复用 Manager 的密钥；" >&2
    echo "独立部署时请从 Manager 的 data/.env 复制该值。" >&2
    exit 2
  fi
}

ensure_data_dir() {
  mkdir -p data
  if [[ -w data ]]; then
    return 0
  fi
  echo "data/ 当前不可写，尝试用免密 sudo 修正所有者" >&2
  if ! command -v sudo >/dev/null 2>&1 || ! sudo -n true 2>/dev/null; then
    echo "错误：无法自动修正 data/ 权限；请手动执行：" >&2
    echo "  sudo chown -R $(id -u):$(id -g) data" >&2
    return 1
  fi
  sudo -n chown -R "$(id -u):$(id -g)" data
}

ensure_image() {
  if image_current; then
    return 0
  fi
  echo "构建 Monitor 镜像"
  docker compose build
  ensure_data_dir
  source_hash > data/image-source-hash
}

ensure_message_file() {
  ensure_data_dir
  [[ -f src/message.yml ]] || return 0
  if [[ -f data/message.yml && ! -w data/message.yml ]]; then
    rm -f data/message.yml
  fi
  if [[ ! -f data/message.yml ]]; then
    cp src/message.yml data/message.yml
    chmod 0644 data/message.yml
  fi
}

prune_images() {
  local container project
  container="$(docker compose ps -q monitor 2>/dev/null || true)"
  [[ -n "$container" ]] || return 0
  project="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project" }}' "$container" 2>/dev/null || true)"
  [[ -n "$project" ]] || return 0
  docker image prune -f --filter "label=com.docker.compose.project=$project" >/dev/null 2>&1 || true
}

case "${1:-help}" in
  check)
    if (( $# != 1 )); then
      echo "用法：./monitor.sh check" >&2
      exit 2
    fi
    check_requirements
    echo "Monitor 运行环境有效"
    ;;
  install)
    recreate=0
    shift
    while (( $# )); do
      case "$1" in
        --recreate) recreate=1 ;;
        *)
          echo "用法：./monitor.sh install [--recreate]" >&2
          exit 2
          ;;
      esac
      shift
    done
    check_requirements
    ensure_manager_secret
    ensure_message_file
    ensure_image
    up_args=(-d --no-build)
    if (( recreate )); then
      up_args+=(--force-recreate)
    fi
    docker compose up "${up_args[@]}"
    prune_images
    ;;
  apply-config)
    check_requirements
    ensure_manager_secret
    ensure_message_file
    ensure_image
    docker compose run --rm --no-deps monitor \
      python -c 'from src.config import load; load()'
    exec docker compose restart monitor
    ;;
  start)
    check_requirements
    ensure_manager_secret
    ensure_message_file
    exec docker compose start monitor
    ;;
  stop)
    check_requirements
    exec docker compose stop monitor
    ;;
  restart)
    check_requirements
    ensure_manager_secret
    ensure_message_file
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
  add-admin)
    check_requirements
    if (( $# > 2 )); then
      echo "用法：./monitor.sh add-admin [四位验证码]" >&2
      exit 2
    fi
    ensure_message_file
    ensure_image
    was_running=false
    if [[ -n "$(docker compose ps --status running --quiet monitor)" ]]; then
      docker compose stop monitor >/dev/null
      was_running=true
    fi
    restore_monitor() {
      if [[ "$was_running" == true ]]; then
        docker compose up -d --force-recreate monitor >/dev/null
      fi
    }
    trap restore_monitor EXIT
    command=(python -m src.app add-admin)
    if [[ -n "${2:-}" ]]; then
      command+=("$2")
    fi
    docker compose run --rm --no-deps monitor "${command[@]}"
    ;;
  test-alert)
    check_requirements
    ensure_message_file
    ensure_image
    exec docker compose run --rm --no-deps monitor python -m src.app test-alert
    ;;
  reset-targets)
    check_requirements
    ensure_message_file
    ensure_image
    if [[ "${2:-}" != "--force" || $# -ne 2 ]]; then
      echo "用法：./monitor.sh reset-targets --force" >&2
      echo "该命令会清空 data/qqbot.json（管理员、订阅用户/群、待审批申请）。" >&2
      exit 2
    fi
    was_running=false
    if [[ -n "$(docker compose ps --status running --quiet monitor)" ]]; then
      docker compose stop monitor >/dev/null
      was_running=true
    fi
    docker compose run --rm --no-deps monitor python -m src.app reset-targets
    if [[ "$was_running" == true ]]; then
      docker compose up -d monitor >/dev/null
    fi
    ;;
  reset-messages)
    if [[ "${2:-}" != "--force" || $# -ne 2 ]]; then
      echo "用法：./monitor.sh reset-messages --force" >&2
      echo "该命令会用 src/message.yml 替换 data/message.yml，并保留时间戳备份。" >&2
      exit 2
    fi
    ensure_data_dir
    [[ -f src/message.yml ]] || {
      echo "错误：缺少 src/message.yml" >&2
      exit 2
    }
    if [[ -f data/message.yml ]]; then
      backup="data/message.yml.$(date +%Y%m%d-%H%M%S).bak"
      cp -- data/message.yml "$backup"
      echo "原消息模板已备份：$backup"
    fi
    temporary="data/.message.yml.$$"
    trap 'rm -f -- "$temporary"' EXIT
    cp -- src/message.yml "$temporary"
    chmod 0644 "$temporary"
    mv -f -- "$temporary" data/message.yml
    trap - EXIT
    echo "QQ 消息模板已重置：data/message.yml"
    ;;
  help|-h|--help)
    echo "用法："
    echo "  ./monitor.sh check"
    echo "  ./monitor.sh install [--recreate]"
    echo "  ./monitor.sh start|stop|restart|status|logs"
    echo "  ./monitor.sh apply-config"
    echo "  ./monitor.sh add-admin [四位验证码]"
    echo "  ./monitor.sh test-alert"
    echo "  ./monitor.sh reset-targets --force"
    echo "  ./monitor.sh reset-messages --force"
    ;;
  *)
    echo "错误：未知命令：$1" >&2
    exit 2
    ;;
esac
