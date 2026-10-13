#!/bin/bash
set -Eeuo pipefail
cd /app
umask 077

python3 - <<'PY'
import os
import pathlib
import shlex

def write_shell(path, names):
    lines = [f"{name}={shlex.quote(os.environ.get(name, ''))}\n" for name in names]
    pathlib.Path(path).write_text("".join(lines), encoding="utf-8")

write_shell("notify.conf", [
    "QQBOT_ENABLED", "QQBOT_SECRET_ID", "QQBOT_SECRET_KEY",
    "QQBOT_TARGET_TYPE", "QQBOT_TARGET", "QQBOT_GROUP_TARGET", "QQBOT_ADMINS",
])
write_shell("cvm-power.runtime.conf", [
    "INSTANCE_ID", "REGION", "MC_PORT", "MC_CONF",
    "POWEROFF_WAIT_MAX", "PORT_CLOSE_WAIT_MAX", "PORT_OPEN_WAIT_MAX",
    "STATE_WAIT_MAX", "GUARD_GRACE",
])
PY

cat > mc.conf <<'EOF'
PLAY_START="${PLAY_START:-20:00}"
PLAY_END="${PLAY_END:-23:00}"
MC_ADDR="${MC_ADDR:-mc.tunarund.top:27015}"
BOOT_LEAD_MIN="${BOOT_LEAD_MIN:-10}"
POWEROFF_LAG_MIN="${POWEROFF_LAG_MIN:-5}"
WATCH_STOP_LEAD_MIN="${WATCH_STOP_LEAD_MIN:-5}"
NOTICE_MINUTES="${NOTICE_MINUTES:-60 30 15 7 3 1}"
__hm() { date -d "@$1" +%H:%M; }
__ps=$(date -d "$(date +%F) $PLAY_START" +%s)
__pe=$(date -d "$(date +%F) $PLAY_END" +%s)
BOOT_AT="${POWER_START:-$(__hm $((__ps - BOOT_LEAD_MIN * 60)))}"
POWEROFF_AT="${POWEROFF_AT:-$(__hm $((__pe + POWEROFF_LAG_MIN * 60)))}"
SHUTDOWN_AT="${SHUTDOWN_AT:-$PLAY_END}"
WATCH_FROM=$(__hm $((__ps - BOOT_LEAD_MIN * 60 + 300)))
WATCH_UNTIL=$(__hm $((__pe - WATCH_STOP_LEAD_MIN * 60)))
MC_CONF_OK=1
unset -f __hm
unset __ps __pe
EOF

python3 messages-to-env.py messages.json > messages.env
python3 qqbot-menu.py --self-test

export CVM_POWER_CONF=/app/cvm-power.runtime.conf
export NOTIFY_CONF=/app/notify.conf
export CVM_POWER_DIR=/app
export CVM_POWER_STATE_DIR=/app/state
export QQBOT_DIR=/app
export LOG_DIR=/app/state/logs

power_scheduler() {
  local tick last marker=/app/state/scheduler.last action
  mkdir -p /app/state
  while sleep 15; do
    tick="$(date '+%F %H:%M')"
    last="$(cat "$marker" 2>/dev/null || true)"
    [ "$tick" != "$last" ] || continue
    printf '%s\n' "$tick" > "$marker"
    action=""
    if [ "$(date +%H:%M)" = "${POWER_START:-19:50}" ]; then
      action="start --scheduled"
    elif [ "$(date +%H:%M)" = "${PLAY_END:-23:00}" ]; then
      action="stop"
    elif [ "$(date +%M)" = 00 ] || [ "$(date +%M)" = 30 ]; then
      action="guard"
    fi
    if [ -n "$action" ]; then
      # shellcheck disable=SC2086
      bash /app/cvm-power.sh $action || true
    fi
  done
}

if [ "${POWER_SCHEDULER_ENABLED:-1}" = 1 ]; then
  power_scheduler &
fi

exec python3 /app/qqbot-menu.py
