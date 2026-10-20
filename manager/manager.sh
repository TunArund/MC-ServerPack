#!/usr/bin/env bash
set -Eeuo pipefail
ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"
IMAGE="mc-serverpack-manager:local"
COMPOSE="$ROOT_DIR/runtime/compose.yml"
SOCKET="$ROOT_DIR/data/run/manager.sock"
die() { echo "错误：$*" >&2; exit 2; }
check() {
  command -v docker >/dev/null 2>&1 || die "需要 Docker Engine"
  docker compose version >/dev/null 2>&1 || die "需要 Docker Compose 插件"
  docker info >/dev/null 2>&1 || die "当前用户无法使用 Docker Engine"
  command -v curl >/dev/null 2>&1 || die "需要 curl"
}
usage() {
  cat <<'EOF'
用法：./manager.sh <命令>

宿主操作：check | install | apply-config | service start|stop|status|logs
整合包：list | status | create | config | start | stop | stop-all
        enable | disable | logs | console | property | backup | restore
        remove-container | remove | export
EOF
}
compose() {
  export MC_CONTAINER_UID="$(id -u)" MC_CONTAINER_GID="$(id -g)"
  export MC_DOCKER_GID="$(stat -c '%g' /var/run/docker.sock)" MCSP_MANAGER_ROOT="$ROOT_DIR"
  docker compose --project-name mc-serverpack-manager -f "$COMPOSE" "$@"
}
ensure_image() {
  docker image inspect "$IMAGE" >/dev/null 2>&1 \
    || die "Manager 镜像不存在；请先执行 ./manager.sh install"
}
bootstrap() {
  # 在控制容器内生成 data/.env 密钥与 data/tls 证书，宿主机不需要 Python 或 openssl。
  docker run --rm --network none --user "$(id -u):$(id -g)" \
    -v "$ROOT_DIR/data:$ROOT_DIR/data" \
    -e HOME=/tmp -e MCSP_MANAGER_ROOT="$ROOT_DIR" \
    "$IMAGE" python -m src.bootstrap
}
json_quote() {
  local value="$1"; value="${value//\\/\\\\}"; value="${value//\"/\\\"}"
  value="${value//$'\n'/\\n}"; value="${value//$'\r'/\\r}"; printf '"%s"' "$value"
}
request() {
  local method="$1" path="$2" body="${3:-}" result
  [[ -S "$SOCKET" ]] || die "Manager 服务未运行；请执行 ./manager.sh install"
  if [[ -n "$body" ]]; then
    result="$(curl --silent --show-error --unix-socket "$SOCKET" -X "$method" \
      -H 'Content-Type: application/json' -H "Idempotency-Key: $(date +%s%N)-$$" \
      --data-binary "$body" "http://localhost$path")" || die "Manager API 请求失败"
  else
    result="$(curl --silent --show-error --unix-socket "$SOCKET" -X "$method" \
      "http://localhost$path")" || die "Manager API 请求失败"
  fi
  if [[ "$result" == *'"ok":false'* ]]; then echo "$result" >&2; return 2; fi
  printf '%s\n' "$result"
}
wait_job() {
  local response="$1" job state seq=0 message
  job="$(sed -n 's/.*"job_id":"\([^"]*\)".*/\1/p' <<<"$response")"
  [[ -n "$job" ]] || { printf '%s\n' "$response"; return; }
  while true; do
    response="$(request GET "/v1/jobs/$job?after=$seq&wait=20")" || return
    while IFS= read -r message; do [[ -n "$message" ]] && printf '%s\n' "$message"; seq=$((seq + 1)); done \
      < <(grep -o '"message":"[^"]*"' <<<"$response" | sed 's/^"message":"//;s/"$//')
    state="$(sed -n 's/.*"state":"\([^"]*\)".*/\1/p' <<<"$response")"
    case "$state" in succeeded) printf '%s\n' "$response"; return 0;; failed|cancelled) echo "$response" >&2; return 2;; esac
  done
}
api_job() { local result; result="$(request "$@")" || return; wait_job "$result"; }
[[ "$(id -u)" -ne 0 ]] || die "请用普通用户运行 ./manager.sh，不要使用 root"
case "${1:-}" in
  -h|--help|help) usage ;;
  check) [[ $# -eq 1 ]] || die "用法：./manager.sh check"; check; echo "Manager 运行环境有效" ;;
  install)
    [[ $# -eq 1 ]] || die "用法：./manager.sh install"; check
    mkdir -p data/{jobs,logs,run,transfers,tls} modpacks; [[ -f data/config.yml ]] || die "缺少 data/config.yml"
    echo "[Manager 1/3] 构建控制容器镜像"; docker build -f runtime/manager.dockerfile -t "$IMAGE" .
    echo "[Manager 2/3] 生成 API 密钥与 TLS 证书"; bootstrap
    echo "[Manager 3/3] 启动控制容器"; compose up -d --force-recreate --wait --wait-timeout 60
    echo "Manager 安装完成" ;;
  apply-config)
    [[ $# -eq 1 ]] || die "用法：./manager.sh apply-config"; check
    ensure_image
    bootstrap
    compose up -d --force-recreate --wait --wait-timeout 60
    echo "Manager 配置已应用" ;;
  service)
    check
    case "${2:-}" in start) compose up -d --wait --wait-timeout 60;; stop) compose stop;; status) compose ps;; logs) compose logs "${@:3}" manager;; *) die "用法：./manager.sh service start|stop|status|logs";; esac ;;
  list) request GET /v1/modpacks ;;
  status) [[ $# -eq 2 ]] || die "用法：./manager.sh status <id>"; request GET "/v1/modpacks/$2" ;;
  create) [[ $# -ge 2 ]] || die "用法：./manager.sh create <id> [--force]"; api_job POST /v1/modpacks "{\"id\":$(json_quote "$2"),\"force\":$([[ ${3:-} == --force ]] && echo true || echo false)}" ;;
  config) [[ $# -eq 2 ]] || die "用法：./manager.sh config <id>"; request POST "/v1/modpacks/$2/validate" '{}' ;;
  start|stop|enable|disable) [[ $# -ge 2 ]] || die "用法：./manager.sh $1 <id> [--now]"; api_job POST "/v1/modpacks/$2/$1" "{\"now\":$([[ ${3:-} == --now ]] && echo true || echo false)}" ;;
  stop-all) api_job POST /v1/modpacks/stop-all '{}' ;;
  logs) [[ $# -ge 2 ]] || die "用法：./manager.sh logs <id> [--lines n]"; request GET "/v1/modpacks/$2/logs?lines=${4:-100}" ;;
  console) [[ $# -ge 3 ]] || die "用法：./manager.sh console <id> <command>"; request POST "/v1/modpacks/$2/console" "{\"command\":$(json_quote "${*:3}")}" ;;
  property) [[ $# -ge 3 ]] || die "用法：./manager.sh property <id> <key> [value]"; if [[ $# -eq 3 ]]; then request GET "/v1/modpacks/$2/properties/$3"; else request PATCH "/v1/modpacks/$2/properties/$3" "{\"value\":$(json_quote "$4")}"; fi ;;
  backup) if [[ ${2:-} == list ]]; then [[ $# -eq 3 ]] || die "用法：./manager.sh backup list <id>"; request GET "/v1/modpacks/$3/backups"; else [[ $# -eq 2 ]] || die "用法：./manager.sh backup <id>"; api_job POST "/v1/modpacks/$2/backups" '{}'; fi ;;
  restore)
    [[ $# -ge 2 ]] || die "用法：./manager.sh restore <id> [backup] [--auto-restart]"
    backup="${3:-}"; [[ "$backup" == --auto-restart ]] && backup=""
    api_job POST "/v1/modpacks/$2/restore" "{\"backup\":$(json_quote "$backup"),\"auto_restart\":$([[ ${4:-} == --auto-restart || ${3:-} == --auto-restart ]] && echo true || echo false)}" ;;
  remove-container) [[ $# -eq 2 ]] || die "用法：./manager.sh remove-container <id>"; api_job DELETE "/v1/modpacks/$2/container" '{}' ;;
  remove) [[ $# -ge 2 ]] || die "用法：./manager.sh remove <id> [--force]"; api_job DELETE "/v1/modpacks/$2" "{\"force\":$([[ ${3:-} == --force ]] && echo true || echo false)}" ;;
  export) [[ $# -eq 2 ]] || die "用法：./manager.sh export <id>"; api_job POST "/v1/modpacks/$2/exports" '{}' ;;
  *) die "用法：./manager.sh {check|install|apply-config|service|list|status|create|config|start|stop|stop-all|enable|disable|logs|console|property|backup|restore|remove-container|remove|export}" ;;
esac
