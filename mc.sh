#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$ROOT/.env"
CONFIG_FILE="$ROOT/config.yml"
CONFIG_TOOL="$ROOT/lib/project_config.py"
PYTHON_BIN="${MC_PYTHON:-python3}"
MANAGER_SOURCE="$ROOT/manager"
GATEWAY_SOURCE="$ROOT/qqbot"
TMP_DIR=""

cleanup() {
  [ -z "$TMP_DIR" ] || rm -rf -- "$TMP_DIR"
}
trap cleanup EXIT

usage() {
  cat <<'EOF'
用法:
  ./mc.sh deploy [--check] manager|gateway|all
  ./mc.sh config    gateway
  ./mc.sh manager   create|remove|start|stop|restart|status|logs|backup|restore|config ...
  ./mc.sh gateway   start|stop|status|guard
  ./mc.sh bot       status|restart|stop|logs|follow [行数]
  ./mc.sh cos       [对象名] [--get] [--expire 秒] [--force] [--url-only]

说明:
  manager 部署到 mc-server；gateway 部署 QQ Bot、定时任务和 CVM 控制器。
  整合包运维统一通过 ./mc.sh manager <命令...> 转发给 manager.sh。
  没有常驻 gateway 时，只执行 deploy manager，云主机由个人 PC 手动开关。
EOF
}

die() { echo "错误: $*" >&2; exit 1; }

require_env() {
  [ -r "$ENV_FILE" ] || die "缺少 $ENV_FILE，请从 .env.example 复制并填写"
}

config_value() { "$PYTHON_BIN" -B "$CONFIG_TOOL" get "$1"; }

env_value() {
  local key="$1" fallback="${2:-}" value
  value="$(awk -F= -v key="$key" '$1 == key {sub(/^[^=]*=/, ""); print; exit}' "$ENV_FILE")"
  value="${value%\"}"; value="${value#\"}"
  value="${value%\'}"; value="${value#\'}"
  printf '%s' "${value:-$fallback}"
}

manager_host() { config_value manager.host; }
gateway_host() { config_value gateway.host; }
manager_dir() { config_value manager.dir; }
gateway_dir() { config_value gateway.dir; }
gateway_pack() { "$PYTHON_BIN" -B "$CONFIG_TOOL" gateway-pack; }

validate_pack() {
  [[ "$1" =~ ^[A-Za-z0-9._-]+$ ]] || die "整合包 ID 只能包含字母、数字、点、下划线和减号"
}

validate_remote_dir() {
  [[ "$1" == /* && "$1" != *"'"* && "$1" != *$'\n'* ]] \
    || die "远端目录必须是无单引号的绝对路径: $1"
}

remote_manager() {
  local host dir remote_command arg quoted
  host="$(manager_host)"
  dir="$(manager_dir)"
  validate_remote_dir "$dir"
  printf -v remote_command 'cd %q && ./manager.sh' "$dir"
  for arg in "$@"; do
    printf -v quoted '%q' "$arg"
    remote_command+=" $quoted"
  done
  ssh -o BatchMode=yes "$host" "$remote_command"
}

install_dependencies() {
  local target="$1" host require_python=0
  case "$target" in
    manager) host="$(manager_host)"; require_python=1 ;;
    gateway) host="$(gateway_host)" ;;
    *) die "未知部署目标: $target" ;;
  esac

  echo "==> [$target] 检查 $host 的运行环境"
  ssh -o BatchMode=yes "$host" "REQUIRE_PYTHON=$require_python bash -s" <<'REMOTE'
set -Eeuo pipefail

need_install=0
command -v docker >/dev/null 2>&1 || need_install=1
docker compose version >/dev/null 2>&1 || need_install=1
command -v rsync >/dev/null 2>&1 || need_install=1
if [ "$REQUIRE_PYTHON" = 1 ]; then
  command -v python3 >/dev/null 2>&1 || need_install=1
  python3 -c 'import yaml' >/dev/null 2>&1 || need_install=1
  command -v crontab >/dev/null 2>&1 || need_install=1
fi
[ "$need_install" = 0 ] && exit 0

if [ "$(id -u)" -eq 0 ]; then
  run_root() { "$@"; }
else
  sudo -n true >/dev/null 2>&1 || {
    echo "当前用户需要免密 sudo 才能安装运行环境" >&2
    exit 1
  }
  run_root() { sudo -n "$@"; }
fi

. /etc/os-release
case "$ID" in debian|ubuntu) ;; *) echo "暂不支持的系统: $ID" >&2; exit 1 ;; esac

run_root apt-get update
packages=(ca-certificates curl gnupg rsync)
if [ "$REQUIRE_PYTHON" = 1 ]; then
  packages+=(python3 python3-yaml cron)
fi
run_root env DEBIAN_FRONTEND=noninteractive apt-get install -y "${packages[@]}"

if ! docker compose version >/dev/null 2>&1; then
  run_root install -m 0755 -d /etc/apt/keyrings
  curl -fsSL "https://download.docker.com/linux/$ID/gpg" -o /tmp/docker.asc
  run_root install -m 0644 /tmp/docker.asc /etc/apt/keyrings/docker.asc
  rm -f /tmp/docker.asc
  arch="$(dpkg --print-architecture)"
  repo="deb [arch=$arch signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/$ID $VERSION_CODENAME stable"
  run_root sh -c "printf '%s\\n' '$repo' > /etc/apt/sources.list.d/docker.list"
  run_root apt-get update
  run_root env DEBIAN_FRONTEND=noninteractive apt-get install -y \
    docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi

run_root systemctl enable --now docker
if [ "$REQUIRE_PYTHON" = 1 ]; then
  run_root systemctl enable --now cron
fi
if [ "$(id -u)" -ne 0 ]; then
  run_root usermod -aG docker "$(id -un)"
fi
REMOTE
}

write_gateway_runtime_env() {
  local output="$1" pack port user_target group_target
  for key in GW_SECRET_ID GW_SECRET_KEY QQBOT_SECRET_ID QQBOT_SECRET_KEY; do
    [ -n "$(env_value "$key")" ] || die ".env 缺少 $key"
  done
  pack="$(gateway_pack)"
  port="$("$PYTHON_BIN" -B "$MANAGER_SOURCE/lib/config.py" pack "$pack" port)"
  user_target="$("$PYTHON_BIN" -B "$CONFIG_TOOL" targets user)"
  group_target="$("$PYTHON_BIN" -B "$CONFIG_TOOL" targets group)"
  {
    printf 'CVM_INSTANCE_ID=%s\n' "$(config_value manager.instance-id)"
    printf 'CVM_REGION=%s\n' "$(config_value manager.region)"
    printf 'CVM_SECRET_ID=%s\n' "$(env_value GW_SECRET_ID)"
    printf 'CVM_SECRET_KEY=%s\n' "$(env_value GW_SECRET_KEY)"
    printf 'QQBOT_SECRET_ID=%s\n' "$(env_value QQBOT_SECRET_ID)"
    printf 'QQBOT_SECRET_KEY=%s\n' "$(env_value QQBOT_SECRET_KEY)"
    printf 'QQBOT_TARGET_TYPE=%s\n' "$([ -n "$user_target" ] && echo user || echo group)"
    printf 'QQBOT_TARGET=%s\n' "${user_target%% *}"
    printf 'QQBOT_GROUP_TARGET=%s\n' "${group_target%% *}"
    printf 'QQBOT_ADMINS=%s\n' "$("$PYTHON_BIN" -B "$CONFIG_TOOL" admins)"
    printf 'MC_PACK=%s\n' "$pack"
    printf 'MC_PORT=%s\n' "$port"
    printf 'MC_ADDR=%s:%s\n' "$(config_value manager.host)" "$port"
    printf 'MC_AUTO_SCHEDULE=%s\n' "$([ "$(config_value gateway.schedule.enabled)" = true ] && echo 1 || echo 0)"
    # qqbot/compose.yml 只消费环境变量；这是由 config.yml 与密钥生成的部署产物，
    # 不是需要用户维护的第二份配置源。
    printf 'MC_POWER_START=%s\n' "$(config_value gateway.schedule.power-start)"
    printf 'MC_PLAY_START=%s\n' "$(config_value gateway.schedule.start)"
    printf 'MC_SHUTDOWN_START=%s\n' "$(config_value gateway.schedule.end)"
    printf 'MC_PLAY_END=%s\n' "$(config_value gateway.schedule.end)"
    printf 'MC_POWEROFF_AT=%s\n' "$(config_value gateway.schedule.poweroff)"
    printf 'MC_NOTICE_MINUTES=%s\n' "$(config_value gateway.schedule.notices)"
    printf 'MC_MANAGER_DIR=%s\n' "$(manager_dir)"
  } > "$output"
  chmod 600 "$output"
}

deploy_manager() {
  local host dir pack
  host="$(manager_host)"
  dir="$(manager_dir)"
  validate_remote_dir "$dir"
  [ -x "$MANAGER_SOURCE/manager.sh" ] || die "缺少 manager/manager.sh"

  install_dependencies manager
  echo "==> [manager] 同步到 $host:$dir"
  ssh -o BatchMode=yes "$host" "mkdir -p '$dir'"
  rsync -az --delete \
    --exclude .env \
    --exclude 'modpacks/*/data/' \
    --exclude 'modpacks/*/backups/' \
    "$MANAGER_SOURCE/" "$host:$dir/"
  rsync -az "$CONFIG_FILE" "$host:$dir/config.yml"

  # 补齐尚不存在的初始 data 文件，但不覆盖远端世界、配置或日志。
  while IFS= read -r pack; do
    [ -n "$pack" ] || continue
    if [ -d "$MANAGER_SOURCE/modpacks/$pack/data" ]; then
      ssh -o BatchMode=yes "$host" "mkdir -p '$dir/modpacks/$pack/data'"
      rsync -az --ignore-existing "$MANAGER_SOURCE/modpacks/$pack/data/" \
        "$host:$dir/modpacks/$pack/data/"
    fi
  done < <("$PYTHON_BIN" -B "$MANAGER_SOURCE/lib/config.py" packs)

  echo "==> [manager] 构建 Java 环境镜像"
  ssh -o BatchMode=yes "$host" "cd '$dir' && for version in 8 17 21; do docker build -f runtime/java\${version}.dockerfile -t mc-java:\${version} .; done"
  echo "==> [manager] 生成整合包配置"
  while IFS= read -r pack; do
    [ -n "$pack" ] || continue
    remote_manager config "$pack"
  done < <("$PYTHON_BIN" -B "$MANAGER_SOURCE/lib/config.py" packs)
}

deploy_gateway() {
  local host dir generated_env
  host="$(gateway_host)"
  dir="$(gateway_dir)"
  validate_remote_dir "$dir"
  [ -f "$GATEWAY_SOURCE/compose.yml" ] || die "缺少 qqbot/compose.yml"
  [ -f "$GATEWAY_SOURCE/Dockerfile" ] || die "缺少 qqbot/Dockerfile"
  require_env

  install_dependencies gateway
  TMP_DIR="$(mktemp -d /tmp/mc-serverpack.XXXXXX)"
  generated_env="$TMP_DIR/gateway.env"
  write_gateway_runtime_env "$generated_env"

  echo "==> [gateway] 同步到 $host:$dir（保留 data/）"
  ssh -o BatchMode=yes "$host" "mkdir -p '$dir/data'"
  rsync -az --delete --exclude data/ --exclude .env \
    "$GATEWAY_SOURCE/" "$host:$dir/"
  rsync -az --chmod=F600 "$generated_env" "$host:$dir/.env"
  ssh -o BatchMode=yes "$host" \
    "cd '$dir' && docker compose config --quiet && docker compose up -d --build --remove-orphans qqbot"
  rm -rf -- "$TMP_DIR"
  TMP_DIR=""
}

deploy_target() {
  case "$1" in
    manager) deploy_manager ;;
    gateway) deploy_gateway ;;
    all) deploy_manager; deploy_gateway ;;
    *) die "目标应为 manager、gateway 或 all" ;;
  esac
}

bot_compose() {
  local dir host action="$1" lines="${2:-100}"
  dir="$(gateway_dir)"; host="$(gateway_host)"
  validate_remote_dir "$dir"
  [[ "$lines" =~ ^[0-9]+$ ]] || die "日志行数必须是非负整数"
  case "$action" in
    status) ssh -o BatchMode=yes "$host" "cd '$dir' && docker compose ps qqbot" ;;
    restart) ssh -o BatchMode=yes "$host" "cd '$dir' && docker compose restart qqbot" ;;
    stop) ssh -o BatchMode=yes "$host" "cd '$dir' && docker compose stop qqbot" ;;
    logs) ssh -o BatchMode=yes "$host" "cd '$dir' && docker compose logs --tail='$lines' qqbot" ;;
    follow) ssh -t "$host" "cd '$dir' && docker compose logs -f --tail='$lines' qqbot" ;;
    *) die "bot 操作应为 status、restart、stop、logs 或 follow" ;;
  esac
}

power_action() {
  local action="$1" dir host pack
  case "$action" in start|stop|status|guard) ;; *) die "未知电源操作: $action" ;; esac
  dir="$(gateway_dir)"; host="$(gateway_host)"
  validate_remote_dir "$dir"
  if [ "$action" = stop ]; then
    pack="$(gateway_pack)"
    validate_pack "$pack"
    echo "==> [manager] 先优雅停止 $pack"
    remote_manager stop "$pack" \
      || die "无法确认 Minecraft 已保存并停止，拒绝关闭云主机"
    ssh -o BatchMode=yes "$host" \
      "cd '$dir' && docker compose exec -T -e POWEROFF_WAIT_MAX=0 qqbot bash /app/cvm-power.sh stop"
  else
    ssh -o BatchMode=yes "$host" \
      "cd '$dir' && docker compose exec -T qqbot bash /app/cvm-power.sh '$action'"
  fi
}

check_target() {
  local target="$1" failed=0 required_command
  for required_command in ssh rsync awk "$PYTHON_BIN"; do
    if command -v "$required_command" >/dev/null 2>&1; then
      echo "[OK] $required_command"
    else
      echo "[FAIL] $required_command"
      failed=1
    fi
  done
  if command -v "$PYTHON_BIN" >/dev/null 2>&1 && "$PYTHON_BIN" -B "$CONFIG_TOOL" validate >/dev/null; then
    echo "[OK] config.yml"
  else
    echo "[FAIL] config.yml"
    failed=1
  fi
  if [ "$target" = manager ] || [ "$target" = all ]; then
    [ -x "$MANAGER_SOURCE/manager.sh" ] && echo "[OK] manager" \
      || { echo "[FAIL] manager"; failed=1; }
    ssh -o BatchMode=yes "$(manager_host)" \
      'command -v docker >/dev/null && docker compose version >/dev/null && command -v rsync >/dev/null && command -v python3 >/dev/null && python3 -c "import yaml" && command -v crontab >/dev/null' \
      && echo "[OK] manager 远端环境" || { echo "[FAIL] manager 远端环境（可直接 deploy 自动安装）"; failed=1; }
  fi
  if [ "$target" = gateway ] || [ "$target" = all ]; then
    [ -r "$ENV_FILE" ] && echo "[OK] .env" || { echo "[FAIL] .env"; failed=1; }
    [ -f "$GATEWAY_SOURCE/compose.yml" ] && echo "[OK] gateway" \
      || { echo "[FAIL] gateway"; failed=1; }
    ssh -o BatchMode=yes "$(gateway_host)" \
      'command -v docker >/dev/null && docker compose version >/dev/null && command -v rsync >/dev/null' \
      && echo "[OK] gateway 远端环境" || { echo "[FAIL] gateway 远端环境（可直接 deploy 自动安装）"; failed=1; }
  fi
  return "$failed"
}

cos_tool() {
  command -v "$PYTHON_BIN" >/dev/null 2>&1 || die "找不到 Python：$PYTHON_BIN"
  require_env
  env \
    TENCENTCLOUD_SECRET_ID="$(env_value COS_SECRET_ID)" \
    TENCENTCLOUD_SECRET_KEY="$(env_value COS_SECRET_KEY)" \
    REGION="$(config_value cos.region)" \
    COS_BUCKET="$(config_value cos.bucket)" \
    COS_UPLOAD_PREFIX="$(config_value cos.prefix)" \
    COS_OBJECT_NAME="$(env_value COS_OBJECT_NAME)" \
    COS_URL_EXPIRE="$(config_value cos.expire)" \
    "$PYTHON_BIN" "$ROOT/cos/cos.py" "$@"
}

[ "$#" -ge 1 ] || { usage; exit 2; }
command="$1"; shift

case "$command" in
  deploy)
    if [ "${1:-}" = --check ]; then
      [ "$#" -eq 2 ] || { usage; exit 2; }
      case "$2" in manager|gateway|all) check_target "$2" ;; *) usage; exit 2 ;; esac
    else
      [ "$#" -eq 1 ] || { usage; exit 2; }
      deploy_target "$1"
    fi
    ;;
  config)
    [ "$#" -eq 1 ] && [ "$1" = gateway ] || { usage; exit 2; }
    require_env
    TMP_DIR="$(mktemp -d /tmp/mc-serverpack.XXXXXX)"
    write_gateway_runtime_env "$TMP_DIR/gateway.env"
    DEPLOY_ENV_FILE="$TMP_DIR/gateway.env" \
      docker compose --env-file "$TMP_DIR/gateway.env" \
      -f "$GATEWAY_SOURCE/compose.yml" config --quiet
    echo "gateway Compose 配置有效"
    ;;
  manager)
    [ "$#" -ge 1 ] || { usage; exit 2; }
    if [ "$1" = create ]; then
      [ "$#" -eq 2 ] || { usage; exit 2; }
      "$MANAGER_SOURCE/manager.sh" create "$2"
    elif [ "$1" = config ] && [ "${2:-}" = --scan ]; then
      [ "$#" -eq 3 ] || { usage; exit 2; }
      "$MANAGER_SOURCE/manager.sh" config --scan "$3"
    else
      remote_manager "$@"
    fi
    ;;
  gateway)
    [ "$#" -eq 1 ] || { usage; exit 2; }
    case "$1" in
      start|stop|status|guard)
        power_action "$1"
        ;;
      *) usage; exit 2 ;;
    esac
    ;;
  bot)
    [ "$#" -ge 1 ] && [ "$#" -le 2 ] || { usage; exit 2; }
    bot_compose "$@"
    ;;
  cos)
    cos_tool "$@"
    ;;
  help|-h|--help)
    usage
    ;;
  *)
    usage
    exit 2
    ;;
esac
