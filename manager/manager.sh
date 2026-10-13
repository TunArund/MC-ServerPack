#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="$ROOT/lib/config.py"; ARCHIVE="$ROOT/lib/archive.py"; BASE="$ROOT/runtime/compose.yml"
CACHE_SCANNER="$ROOT/lib/cache_scanner.py"
die(){ echo "错误：$*" >&2; exit 1; }
usage(){ cat <<'EOF'
用法：
  ./manager.sh create <modpack-id>
  ./manager.sh remove [--force] <modpack-id>
  ./manager.sh config [modpack-id] | config --scan <modpack-id>
  ./manager.sh start|stop|restart <modpack-id>
  ./manager.sh status [modpack-id]
  ./manager.sh logs [-f] <modpack-id> [行数]
  ./manager.sh backup <modpack-id>
  ./manager.sh restore <modpack-id> [备份文件名]
EOF
}
command -v python3 >/dev/null || die "缺少 Python 3.8+，请通过 mc.sh deploy manager 安装"
python3 -c 'import sys, yaml; raise SystemExit(sys.version_info < (3, 8))' >/dev/null 2>&1 \
  || die "manager 需要 Python 3.8+ 和 PyYAML"
cfg(){ python3 -B "$CONFIG" "$@"; }; global(){ cfg global "$1"; }; value(){ cfg pack "$1" "$2"; }
packs(){ cfg packs; }; require_pack(){ cfg validate "$1"; }; archive(){ python3 -B "$ARCHIVE" "$@"; }
require_docker(){ command -v docker >/dev/null && docker compose version >/dev/null || die "缺少 Docker/Compose"; }
render(){ cfg render "$1" >/dev/null; }
compose(){
  local p="$1" uid gid
  shift; render "$p"
  uid="${MC_CONTAINER_UID:-$(id -u)}"; gid="${MC_CONTAINER_GID:-$(id -g)}"
  [[ "$uid" =~ ^[0-9]+$ && "$gid" =~ ^[0-9]+$ ]] || die "容器 UID/GID 必须是整数"
  [ "$uid" != 0 ] || die "拒绝以 root 运行 Minecraft；请使用普通用户或手动指定 MC_CONTAINER_UID/GID"
  MC_CONTAINER_UID="$uid" MC_CONTAINER_GID="$gid" MC_STOP_TIMEOUT="$(global stop-timeout-seconds)" \
    docker compose --project-name "mc-$p" --project-directory "$ROOT/modpacks/$p" \
    -f "$BASE" -f "$ROOT/modpacks/$p/compose.yml" "$@"
}
running(){ [ -n "$(compose "$1" ps --status running -q minecraft 2>/dev/null || true)" ]; }

create_pack(){
  local p="$1" d="$ROOT/modpacks/$1"; [[ "$p" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || die "无效 ID：$p"
  [ ! -e "$d" ] || die "目录已存在：$d"; mkdir -p "$d"/{src,data,backups}
  cp "$ROOT/modpacks/template/config.yml" "$d/config.yml"; sed -i "s/^name:.*/name: $p/" "$d/config.yml"
  touch "$d/src/.gitkeep" "$d/data/.gitkeep" "$d/backups/.gitkeep"
  echo "已创建：$p"
  echo "下一步：把服务端文件放入 modpacks/$p/src/，再编辑 config.yml"
}
remove_pack(){
  local force=0 p d; [ "${1:-}" != --force ] || { force=1; shift; }; [ "$#" = 1 ] || die "用法：remove [--force] <ID>"
  p="$1"; require_pack "$p"; [ "$p" != template ] || die "不能删除 template"; d="$ROOT/modpacks/$p"
  ! running "$p" || die "请先停止 $p"
  if [ "$force" = 0 ] && find "$d/data" "$d/backups" -type f ! -name .gitkeep -print -quit | grep -q .; then die "含持久化数据；确认后使用 --force"; fi
  compose "$p" down --remove-orphans; rm -rf -- "$d"; echo "已删除：$p"
}
sync_cron(){
  local p="$1" mark tmp exp cmd; command -v crontab >/dev/null || return 0; mark="# mc-manager-backup:$p"; tmp="$(mktemp)"
  crontab -l 2>/dev/null | grep -Fv "$mark" >"$tmp" || true
  if [ "$(value "$p" backup-enabled)" = true ]; then exp="$(value "$p" backup-cron-expression)"; cmd="cd $(printf %q "$ROOT") && ./manager.sh backup $(printf %q "$p") >> $(printf %q "$ROOT/modpacks/$p/data/backup.log") 2>&1"; printf '%s %s %s\n' "$exp" "$cmd" "$mark" >>"$tmp"; fi
  crontab "$tmp"; rm -f -- "$tmp"
}
config_pack(){ require_pack "$1"; render "$1"; sync_cron "$1"; echo "配置有效：$1"; }
config_all(){ local p; while IFS= read -r p; do [ -z "$p" ] || config_pack "$p"; done < <(packs); }
running_packs(){ local p; while IFS= read -r p; do [ -z "$p" ] || ! running "$p" || echo "$p"; done < <(packs); }
check_start(){
  local p="$1" n max policy other port; running "$p" && return 0; max="$(global max-running)"; policy="$(global overflow-policy)"; n="$(running_packs | wc -l)"
  if [ "$n" -ge "$max" ]; then [ "$policy" = rotate ] || die "已达运行上限 $max"; other="$(running_packs | head -1)"; [ -n "$other" ] || die "无法轮换"; compose "$other" stop minecraft; fi
  port="$(value "$p" port)"; while IFS= read -r other; do [ "$other" = "$p" ] || [ "$(value "$other" port)" != "$port" ] || die "端口 $port 已被 $other 使用"; done < <(running_packs)
}
start_pack(){ require_docker; require_pack "$1"; check_start "$1"; compose "$1" up -d --wait --wait-timeout "$(global start-timeout-seconds)"; }
stop_pack(){ require_docker; require_pack "$1"; compose "$1" stop minecraft; }
restart_pack(){ require_docker; require_pack "$1"; compose "$1" restart minecraft; compose "$1" up -d --wait --wait-timeout "$(global start-timeout-seconds)"; }
console(){ printf '%s\n' "$2" | compose "$1" exec -T minecraft sh -c 'cat > /tmp/mc-manager-console.pipe'; }
backup_pack(){
  local p="$1" d world out retain i save=0; local -a files; require_pack "$p"; d="$ROOT/modpacks/$p"; world="$d/data/world"; [ -d "$world" ] || die "没有 data/world"
  mkdir -p "$d/backups"; out="$d/backups/$p-$(date +%Y%m%d-%H%M%S).tar.gz"
  if running "$p"; then console "$p" $'save-off\nsave-all flush'; save=1; sleep 5; fi
  tar -czf "$out" -C "$d/data" world || { rm -f -- "$out"; return 1; }; [ "$save" = 0 ] || console "$p" save-on || true
  retain="$(value "$p" backup-retain)"; mapfile -t files < <(find "$d/backups" -maxdepth 1 -type f -name "$p-*.tar.gz" -printf '%T@ %p\n' | sort -nr | cut -d' ' -f2-)
  for ((i=retain;i<${#files[@]};i++)); do rm -f -- "${files[$i]}"; done; echo "备份完成：$out"
}
restore_pack(){
  local p="$1" name="${2:-}" d src tmp old; require_pack "$p"; ! running "$p" || die "请先停止 $p"; d="$ROOT/modpacks/$p"
  if [ -n "$name" ]; then [ "$(basename -- "$name")" = "$name" ] || die "只能指定文件名"; src="$d/backups/$name"; else src="$(find "$d/backups" -maxdepth 1 -type f -name "$p-*.tar.gz" -printf '%T@ %p\n' | sort -n | tail -1 | cut -d' ' -f2-)"; fi
  [ -f "$src" ] || die "找不到备份"; tmp="$(mktemp -d "$d/.restore.XXXXXX")"; old="$d/.world-before-restore.$$"; archive "$src" "$tmp" || { rm -rf -- "$tmp"; return 1; }
  [ ! -d "$d/data/world" ] || backup_pack "$p"; [ ! -e "$d/data/world" ] || mv "$d/data/world" "$old"
  if mv "$tmp/world" "$d/data/world"; then rm -rf -- "$old" "$tmp"; echo "回档完成：$p"; else [ ! -e "$old" ] || mv "$old" "$d/data/world"; rm -rf -- "$tmp"; return 1; fi
}

case "${1:-help}" in
  help|-h|--help) usage;;
  create) [ "$#" = 2 ] || die "用法：create <ID>"; create_pack "$2";;
  remove) shift; remove_pack "$@";;
  config)
    if [ "${2:-}" = --scan ]; then [ "$#" = 3 ] || die "用法：config --scan <ID>"; python3 -B "$CACHE_SCANNER" "$3"
    elif [ "$#" = 1 ]; then config_all
    elif [ "$#" = 2 ]; then config_pack "$2"
    else die "用法：config [ID] | config --scan <ID>"; fi;;
  start|stop|restart) [ "$#" = 2 ] || die "用法：$1 <ID>"; "${1}_pack" "$2";;
  status) require_docker; if [ "$#" = 2 ]; then require_pack "$2"; compose "$2" ps; else [ "$#" = 1 ] || die "用法：status [ID]"; while IFS= read -r p; do echo "==> $p"; compose "$p" ps; done < <(packs); fi;;
  logs) shift; follow=0; [ "${1:-}" != -f ] || { follow=1; shift; }; [ "$#" -ge 1 ] && [ "$#" -le 2 ] || die "用法：logs [-f] <ID> [行数]"; [[ "${2:-100}" =~ ^[0-9]+$ ]] || die "行数无效"; if [ "$follow" = 1 ]; then compose "$1" logs -f --tail "${2:-100}" minecraft; else compose "$1" logs --tail "${2:-100}" minecraft; fi;;
  backup) [ "$#" = 2 ] || die "用法：backup <ID>"; backup_pack "$2";;
  restore) [ "$#" -ge 2 ] && [ "$#" -le 3 ] || die "用法：restore <ID> [文件]"; restore_pack "$2" "${3:-}";;
  *) die "未知命令：$1";;
esac
