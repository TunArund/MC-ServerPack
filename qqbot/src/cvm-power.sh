#!/bin/bash
# ══════════════════════════════════════════════════════════════════
#  落幕曲 MC 服务器 —— CVM 定时开关机 (在 Lighthouse 上执行)
#
#  为什么要在另一台机器上调 API, 而不是在 CVM 内部关机:
#    在 OS 里 poweroff 只关掉系统, 实例在腾讯云层面仍是「运行中」,
#    按量计费照跑。只有 StopInstances + StoppedMode=STOP_CHARGING
#    才算「关机不收费」。所以必须有一台 CVM 之外的机器来执行 ——
#    以前是 SCF, 现在是这台 Lighthouse (见 README「部署要点」)。
#
#  用法:
#    bash cvm-power.sh start    # 开机, 并等到 27015 端口真的能连上
#                               #   --scheduled: 这次是容器定时器发的,
#                               #   带它才会看「今晚取消过预订」那个标记
#    bash cvm-power.sh stop     # 等到断电时刻后关机并停止计费
#    bash cvm-power.sh status   # 只查状态, 不做任何变更
#    bash cvm-power.sh guard    # 值守检查: 该关没关 / 该开没开, 只读+告警
#    bash cvm-power.sh cancel   # 今晚不玩了: 记下"今天已取消"(定时器不再自动开机)
#                               # + 值守检查静音到今晚结束。只写本地文件。
#    bash cvm-power.sh snooze <告警类型> <秒>
#                               # 忽略值守检查一段时间(guard 的告警上的
#                               # 「忽略 1/2/5h」按钮就是调它)。只写本地文件。
#
#  关于 guard —— 它为什么一定要由【独立调度】触发:
#    start/stop 的失败是自曝的(19:50 没开机, 20:00 玩家就投诉了),
#    但 stop 的失败是【静默且持续烧钱】的: 实例会一路 RUNNING 到第二天,
#    24 小时 vs 3.25 小时, 差 7 倍。而这类失败恰恰包含「stop 调度
#    根本没跑」—— Lighthouse 重启过、容器没起来、tccli 凭据过期。
#    那些情况下 stop 里的 notify() 压根没有机会执行。
#    所以 guard 必须由独立的周期任务拉动, 只依赖「Lighthouse 活着」。
#
#  时间线上所有时刻(开机 / 广播 / 关服 / 断电)都由 mc.conf 按游玩窗口
#  推导, 本文件里【不重复】任何具体时刻 —— 改窗口只改根 .env，再运行
#  mc.sh deploy gateway。以前这些值散在两个机器的四个文件里, 改一处忘一处
#  是真实发生过的事故。
#
#  为什么关机要「先触发、再等到点」而不是直接把调度定在 23:05:
#    两者效果等价, 但这样时间线在日志里是完整的 —— 23:00 能看到脚本
#    确实被拉起来了, 而不是到 23:05 才发现什么都没发生。
#
#  三类动作由 QQ Bot 容器入口中的调度循环触发：定时 start、定时 stop、
#  每半小时 guard。guard 与开关机动作保持独立，才能发现调度失败。
#
#  退出码: 0=成功/无需操作, 1=失败(告警渠道接上后会同时推消息)
# ══════════════════════════════════════════════════════════════════

set -uo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF="${CVM_POWER_CONF:-$SELF_DIR/cvm-power.conf}"
QQBOT_DIR="${QQBOT_DIR:-$SELF_DIR/../qqbot}"
[ -d "$QQBOT_DIR" ] || QQBOT_DIR="$SELF_DIR"
STATE_DIR="${CVM_POWER_STATE_DIR:-$SELF_DIR}"
mkdir -p "$STATE_DIR"

# tccli 使用镜像内的绝对路径，避免容器运行环境的 PATH 差异。
TCCLI="${TCCLI:-$HOME/.local/bin/tccli}"

# ── 配置 ──────────────────────────────────────────────────────────
# shellcheck source=/dev/null
[ -r "$CONF" ] && . "$CONF"

# ── 文案 ──────────────────────────────────────────────────────────
# messages.json 是唯一来源, 容器启动时摊平成 messages.env
# (见 messages-to-env.py)。运行时不依赖 python3 —— 只为发一条告警去起个
# python3 进程, 是这套设计特意要避免的。
# shellcheck source=/dev/null
[ -r "$QQBOT_DIR/mc-msg.sh" ] && . "$QQBOT_DIR/mc-msg.sh"
# shellcheck source=/dev/null
[ -r "$QQBOT_DIR/messages.env" ] && . "$QQBOT_DIR/messages.env"

INSTANCE_ID="${INSTANCE_ID:-ins-3rnx3mue}"
REGION="${REGION:-ap-nanjing}"
MC_PORT="${MC_PORT:-27015}"

# 开机后等 27015 可连的上限(秒)。264 个模组冷启动本来就要几分钟;
# 给到 10 分钟, 正好覆盖 开机→玩家窗口 整段余量 —— 端口一开就立刻
# 返回, 所以调大没有任何代价, 只是让「开机成功」的信号更可信。
PORT_OPEN_WAIT_MAX="${PORT_OPEN_WAIT_MAX:-600}"
# 等实例状态翻转(RUNNING/STOPPED)的上限(秒)
STATE_WAIT_MAX="${STATE_WAIT_MAX:-180}"
# 普通关机失败、确认 Minecraft 端口已关闭后，强制断电的二次检查上限。
FORCE_STATE_WAIT_MAX="${FORCE_STATE_WAIT_MAX:-60}"
# 手动跑 stop 时, 最多愿意为断电时刻等多久(秒)。超过就直接关 ——
# 下午手动跑一次 stop 不该傻等几个小时。
POWEROFF_WAIT_MAX="${POWEROFF_WAIT_MAX:-900}"
# 兜底: 过了断电时刻若 27015 还在监听(说明 CVM 侧关服卡住了),
# 再多等这么久。这只是保险, 正常永远走不到。
PORT_CLOSE_WAIT_MAX="${PORT_CLOSE_WAIT_MAX:-180}"
# 值守检查的宽限(秒): 避开开机/关机的过渡期。开机后 start 自己还要等
# 端口(最多 PORT_OPEN_WAIT_MAX), 所以宽限别小于它。
GUARD_GRACE="${GUARD_GRACE:-600}"

# ── 时刻: 全部来自 mc.conf ───────────────────────────────────
# 断电时刻(POWEROFF_AT)、"该开着"的时段(BOOT_AT–POWEROFF_AT)都在那儿,
# 按游玩窗口推导。改窗口只改根 .env 并运行 mc.sh deploy gateway ——
# 以前这些值散在这台机器和 CVM 的四个文件里。
MC_CONF="${MC_CONF:-$SELF_DIR/mc.conf}"
# shellcheck source=/dev/null
[ -r "$MC_CONF" ] && . "$MC_CONF"

LOG_DIR="${LOG_DIR:-$STATE_DIR/logs}"
LOG_FILE="$LOG_DIR/cvm-power.log"

mkdir -p "$LOG_DIR"

# ── 日志 ──────────────────────────────────────────────────────────
log() {
  local line="[$(date '+%F %T')] $*"
  echo "$line"
  # 顺带进 journald, 方便 journalctl -t cvm-power 回看
  logger -t cvm-power -- "$*" 2>/dev/null || true
  # 简易轮转: 超过 1MB 就换一份, 只留一代
  if [ -f "$LOG_FILE" ] && [ "$(stat -c%s "$LOG_FILE" 2>/dev/null || echo 0)" -gt 1048576 ]; then
    mv -f "$LOG_FILE" "$LOG_FILE.1"
  fi
  echo "$line" >> "$LOG_FILE"
}

# ── 通知 ──────────────────────────────────────────────────────────
# 实际发送交给同目录的 mc-notify.sh(QQ 机器人)。它没配好时静默跳过 ——
# 告警通道不该让开关机流程跟着失败; 发送失败也只记日志, 不影响主流程。
_push() {  # $1=alert|group (发到哪)  $2=来源标签  $3=正文  $4..=按钮"标签:指令"(可多个)
  local sender="$QQBOT_DIR/mc-notify.sh"
  [ -x "$sender" ] || return 0
  local where="$1" label="$2" body="$3"; shift 3
  local args=()
  [ "$where" = "group" ] && args+=(--group)
  local b
  for b in "$@"; do args+=(--button "$b"); done
  "$sender" "${args[@]}" "$label" "$body" || log "通知发送失败(不影响主流程)"
  return 0
}
# 出事了 —— 需要人来看。发【私聊】, 只给运维, 不打扰玩家。
notify()   { log "告警: $*"; _push alert "$(msg ALERT_LABEL_POWER)" "$*"; }
# 日常播报(比如开服成功)—— 玩家要看的。群目标配好之前会退回私聊。
#
# CVM_POWER_QUIET=1 时只记日志、不推送: QQ 菜单守护进程发起开机时会带上它。
# 因为这条播报走的是【主动推送】, 群里"主动推送"开关没开就发不出去
# (40034105); 改由守护进程在被动回复窗口里 @ 用户 —— 那条路不受开关限制。
# 定时任务不带这个变量, 行为完全不变。
announce() {
  log "$*"
  if [ "${CVM_POWER_QUIET:-0}" = "1" ]; then
    return 0
  fi
  _push group "$(msg ALERT_LABEL_ANNOUNCE)" "$*"
}

# ── 「忽略」值守检查 ──────────────────────────────────────────────
# guard 每 30 分钟跑一次, 条件不变就会每 30 分钟喊一次。该安静多久由【值班的
# 人】定: 两条告警上都挂着「忽略 1/2/5h」的按钮, 点一下就把截止时刻写进文件,
# 这段时间里 guard 只记日志、不推手机(子命令就是 cvm-power.sh snooze)。
#
# ⚠️ 忽略的粒度是【整个值守检查】(键就叫 guard), 不是某一条告警 —— 点它的人
# 想说的是"这台机器的事我知道了, N 小时别再报"。两类情况(该关却在烧钱 /
# 该开却没开)在同一时刻互斥, 所以这样不会漏掉另一类。
# 菜单那边写这个键, 自检里有一条守卫盯着"两边写/读的是同一个键"。
#
# 以前这里是【固定 6 小时】去重, 两头都不对: 手动开机测个把小时的东西, 它
# 静不下来; 真出事时又可能被静默吞掉 6 小时。现在只有人点过才安静, 没点就
# 一直催 —— 吵是吵, 但绝不会假装没问题。
#
# ⚠️ 忽略的是【打扰】, 不是问题本身: 日志照记、退出码照旧。
SNOOZE_FILE="$STATE_DIR/.alert-snooze"
# 读和写都用它 —— QQ 菜单点「忽略」时写的也是这个键, 改名要两边一起改。
SNOOZE_KEY="guard"

snooze_until() {  # $1=告警类型。打印忽略截止的 epoch, 没忽略过就是空
  # 文件还不存在(一次都没点过忽略)是正常情况, 不是错误 —— 那时 awk 会因为
  # 打不开文件而非 0 退出, 在 pipefail 下还会传给调用方, 所以这里明确吞掉:
  # 查询函数只该回答"有没有", 不该用退出码表达"文件不在"。
  local stop_at
  stop_at=$(awk -v k="$1" '$1==k {print $2}' "$SNOOZE_FILE" 2>/dev/null | tail -1)
  printf '%s\n' "$stop_at"
}

snoozed() {  # $1=告警类型。还在忽略期内返回 0
  local stop_at
  stop_at=$(snooze_until "$1")
  [ -n "$stop_at" ] && [ "$(date +%s)" -lt "$stop_at" ]
}

snooze_set() {  # $1=告警类型  $2=秒。打印截止时刻
  local stop_at=$(( $(date +%s) + $2 ))
  # 同 key 覆盖 —— 点了 5h 又改点 1h 就是 1h, 文件也不会越积越长
  { grep -v "^$1 " "$SNOOZE_FILE" 2>/dev/null; printf '%s %s\n' "$1" "$stop_at"; } \
    > "$SNOOZE_FILE.tmp" 2>/dev/null && mv -f "$SNOOZE_FILE.tmp" "$SNOOZE_FILE" 2>/dev/null
  printf '%s\n' "$stop_at"
}

# ── 「取消今晚」 ──────────────────────────────────────────────────
# 菜单私聊里那个按钮: 今晚不玩了。三件事, 顺序有讲究:
#   1. 机器开着就先【优雅关掉】—— 那一步由 QQ 菜单那边做(TAT 让服务端存盘),
#      这里不管; 关不掉就不该往下走(见那边的 do_cancel);
#   2. 写下今天的日期, 让当天的定时开机不再执行;
#   3. 值守检查静音到今晚结束 —— 否则 21:00 起 guard 会一直喊"该开却没开"。
#
# ⚠️ 只有带 --scheduled 的自动路径看这个标记。手动/菜单点「开机」
# 是明确的覆盖动作, 照开 —— 否则"取消过一次就再也开不起来"会变成一个坑。
# 标记里存的是【日期】, 所以过期自动失效(明天 19:50 照常开机), 不会忘了解除。
CANCEL_FILE="$STATE_DIR/.cancel-tonight"

cancelled_tonight() {   # 今天写过取消标记吗
  [ "$(cat "$CANCEL_FILE" 2>/dev/null)" = "$(date +%F)" ]
}

cmd_cancel() {
  local today secs
  today=$(date +%F)
  printf '%s\n' "$today" > "$CANCEL_FILE" 2>/dev/null || true
  # 静音到今晚 23:59:59 —— 比 guard 的看护时段(23:05 加宽限)更宽; 过了零点
  # 本来就该恢复正常, 那时"机器关着"是对的, 不会再报。
  secs=$(( $(date -d 'today 23:59:59' +%s) - $(date +%s) ))
  [ "$secs" -lt 0 ] && secs=0
  snooze_set "$SNOOZE_KEY" "$secs" >/dev/null
  log "已取消今晚($today): 不自动开机, 值守检查静音 ${secs}s"
  return 0
}

# ── 最近一次【成功】的开关机 ──────────────────────────────────────
# QQ 菜单守护进程靠它做冷却判断("刚开机就关机"多半是误操作)。
# 为什么记在文件里而不是那个进程的内存里: 定时 start 也得
# 被看见 —— 否则这个保护对定时开机完全无效, 而定时开机恰恰是每天唯一
# 一次"没人按按钮的开机"。
# 只记成功的: 失败/被 flock 挡掉的不算, 否则重试会被自己刚写的记录挡住。
STATE_FILE="$STATE_DIR/last-power-op"
record_op() {  # $1=start|stop
  printf '%s %s\n' "$1" "$(date +%s)" > "$STATE_FILE" 2>/dev/null || true
}

# ── 云 API 封装 ───────────────────────────────────────────────────
# 取 DescribeInstances 返回里的单个字段。用 python3 解析 ——
# 本机没装 jq, 也不想为此多一个依赖。
parse_instance() {
  python3 -c '
import sys, json
field = sys.argv[1]
try:
    d = json.load(sys.stdin)
    s = d.get("InstanceSet") or []
    if not s:
        print(""); sys.exit(0)
    v = s[0].get(field)
    if isinstance(v, list):
        print(v[0] if v else "")
    elif v is None:
        print("")
    else:
        print(v)
except Exception:
    print("")
' "$1"
}

describe() {
  "$TCCLI" cvm DescribeInstances \
    --InstanceIds "[\"$INSTANCE_ID\"]" \
    --region "$REGION" --output json 2>/dev/null
}

get_state() { describe | parse_instance InstanceState; }
get_ip()    { describe | parse_instance PublicIpAddresses; }

# ── 定时等待 ──────────────────────────────────────────────────────
# 等到当天的 HH:MM。已经过点就立刻返回(手动在 23:20 跑 stop 时不该等);
# 距离太远也不等(手动在下午跑 stop 时不该傻等到晚上)。
wait_until() {  # $1=HH:MM。返回 1 = 格式不对, 调用方必须中止而不是照关
  local target now left
  target=$(date -d "$(date +%F) $1" +%s 2>/dev/null)
  if [ -z "$target" ]; then
    log "断电时刻格式不对: $1 (应为 HH:MM)"
    return 1
  fi
  now=$(date +%s)
  left=$((target - now))
  if [ "$left" -le 0 ]; then
    return 0
  fi
  if [ "$left" -gt "$POWEROFF_WAIT_MAX" ]; then
    log "距 $1 还有 $((left / 60)) 分钟, 超过 POWEROFF_WAIT_MAX(${POWEROFF_WAIT_MAX}s) —— 判定为手动执行, 不等待"
    return 0
  fi
  log "等 $1 到点再断电 (还有 $((left / 60)) 分 $((left % 60)) 秒)"
  while [ "$(date +%s)" -lt "$target" ]; do sleep 5; done
  log "已到 $1"
  return 0
}

# ── 端口探测 ──────────────────────────────────────────────────────
# 用 bash 内建的 /dev/tcp, 不依赖 nc(本机没装)。
port_open() {  # $1=host $2=port, 返回 0 表示能连上
  timeout 5 bash -c "exec 3<>/dev/tcp/$1/$2" 2>/dev/null
}

wait_port() {  # $1=host $2=open|closed $3=上限秒数
  # deadline 必须单独一行: local 是命令, 参数会先一次性展开, 写在同一行
  # 的话 $max 还没赋值就被 $((...)) 引用, set -u 下直接报 unbound
  local ip="$1" want="$2" max="$3"
  local deadline=$((SECONDS + max))
  while [ "$SECONDS" -lt "$deadline" ]; do
    if [ "$want" = "open" ]; then
      port_open "$ip" "$MC_PORT" && return 0
    else
      port_open "$ip" "$MC_PORT" || return 0
    fi
    sleep 15
  done
  # 到期前再确认一次, 避免刚好卡在边界
  if [ "$want" = "open" ]; then port_open "$ip" "$MC_PORT"; else ! port_open "$ip" "$MC_PORT"; fi
}

# ── 动作 ──────────────────────────────────────────────────────────
cmd_status() {
  local state ip
  state=$(get_state); ip=$(get_ip)
  if [ -z "$state" ]; then
    log "查询失败: 拿不到实例 $INSTANCE_ID 的状态(检查 tccli 凭据与网络)"
    return 1
  fi
  log "实例 $INSTANCE_ID 状态=$state 公网IP=${ip:-无}"
  if [ "$state" = "RUNNING" ] && [ -n "$ip" ]; then
    if port_open "$ip" "$MC_PORT"; then log "MC $MC_PORT 可连"; else log "MC $MC_PORT 不可连"; fi
  fi
  return 0
}

cmd_start() {
  local state
  # 过期的取消标记顺手清掉 —— 留着它只会让下一次读日志的人以为"又取消了"
  if [ -e "$CANCEL_FILE" ] && ! cancelled_tonight; then
    log "清掉过期的取消标记: $(cat "$CANCEL_FILE" 2>/dev/null)"
    rm -f "$CANCEL_FILE"
  fi
  # 今晚取消过预订, 就不自动开机 —— 只看带 --scheduled 的自动路径
  if [ "${SCHEDULED:-0}" = "1" ] && cancelled_tonight; then
    log "今晚($(cat "$CANCEL_FILE" 2>/dev/null))已取消预订 —— 这次定时开机跳过"
    return 0
  fi
  state=$(get_state)
  [ -z "$state" ] && { notify "$(msg ALERT_START_QUERY_FAILED)"; return 1; }

  if [ "$state" = "RUNNING" ]; then
    log "实例已在运行, 无需开机"
  else
    log "提交开机请求 (当前状态 $state)"
    if ! "$TCCLI" cvm StartInstances \
          --InstanceIds "[\"$INSTANCE_ID\"]" \
          --region "$REGION" --output json >/dev/null 2>&1; then
      notify "$(msg ALERT_START_API_FAILED log_file="$LOG_FILE")"
      return 1
    fi
    # 等状态翻到 RUNNING
    local deadline=$((SECONDS + STATE_WAIT_MAX))
    while [ "$SECONDS" -lt "$deadline" ]; do
      state=$(get_state)
      [ "$state" = "RUNNING" ] && break
      sleep 5
    done
    if [ "$state" != "RUNNING" ]; then
      notify "$(msg ALERT_START_NOT_RUNNING waited="${STATE_WAIT_MAX}" state="$state")"
      return 1
    fi
    log "实例已 RUNNING"
  fi

  # 实例 RUNNING ≠ 玩家能连。264 个模组冷启动要几分钟, 这里等到
  # 端口真的开的时刻 —— 这才是「开机成功」的真实定义。
  local ip; ip=$(get_ip)
  if [ -z "$ip" ]; then
    log "拿不到公网 IP, 跳过端口探测(实例已运行)"
    return 0
  fi
  log "等 MC $ip:$MC_PORT 可连 (最多 ${PORT_OPEN_WAIT_MAX}s)"
  if wait_port "$ip" open "$PORT_OPEN_WAIT_MAX"; then
    log "MC 已就绪, 玩家可以进了"
    # 端口真的开起来才算"开机成功" —— 所以记录点在这儿, 不在 StartInstances
    # 返回时(那时实例 RUNNING 但服务端可能根本起不来)。
    record_op start
    # 这是「开服通知」的唯一正确触发点: 实例 RUNNING 不等于玩家能进,
    # 端口真的可连才算。
    #
    # 正文只写"可以进了": 来源标签那行已经是「🎮 服务器已开服」, 正文再抄
    # 一遍就成了同一句话两遍(踩过)。
    #
    # 地址另起一行拼上去, 口径和 QQ 菜单回「状态」时【完全一致】—— 同一个
    # MSG_STATUS_ADDR。裸的 域名:端口 不是 URL: 要到开放平台后台登记的是
    # http(s) 链接, mc-notify.sh 兜的也正是那种, 所以这行不带 scheme 反而
    # 更稳。
    local body; body="$(msg ALERT_ANNOUNCE_STARTED)"
    [ -n "${MC_ADDR:-}" ] && body="$body"$'\n'"$(msg STATUS_ADDR addr="$MC_ADDR")"
    announce "$body"
    return 0
  fi
  notify "$(msg ALERT_START_PORT_CLOSED waited="${PORT_OPEN_WAIT_MAX}" port="$MC_PORT")"
  return 1
}

cmd_stop() {
  local state safe_to_force=0
  state=$(get_state)
  [ -z "$state" ] && { notify "$(msg ALERT_STOP_QUERY_FAILED)"; return 1; }

  if [ "$state" = "STOPPED" ]; then
    log "实例已关机, 无需操作"
    return 0
  fi

  # 一、固定等到断电时刻。CVM 侧在 PLAY_END 开始优雅关服, 世界落盘要
  # 几十秒到几分钟 —— 不猜、不看信号, 死等到点。
  #
  # 时刻拿不到时不猜: 猜早了会正好切在存档中途。宁可继续计费等人来看,
  # 也不冒丢存档的险 —— 这套系统的取舍一贯是"拿几分钱换确定性"。
  if [ -z "${POWEROFF_AT:-}" ]; then
    notify "$(msg ALERT_STOP_NO_TIME conf="$MC_CONF" script="$SELF_DIR/cvm-power.sh")"
    return 1
  fi
  if ! wait_until "$POWEROFF_AT"; then
    notify "$(msg ALERT_STOP_BAD_TIME time="$POWEROFF_AT" conf="$MC_CONF")"
    return 1
  fi

  # 二、兜底: 都到点了 27015 还在监听, 说明 CVM 侧的关服卡住了。
  # 正常永远走不到这里(那时端口早关了), 走到就是服务端有问题。
  local ip; ip=$(get_ip)
  if [ -n "$ip" ]; then
    if port_open "$ip" "$MC_PORT"; then
      log "$MC_PORT 在 $POWEROFF_AT 之后仍在监听, manager 关服可能卡住, 再等 (最多 ${PORT_CLOSE_WAIT_MAX}s)"
      if wait_port "$ip" closed "$PORT_CLOSE_WAIT_MAX"; then
        safe_to_force=1
      else
        notify "$(msg ALERT_STOP_PORT_STILL_OPEN time="$POWEROFF_AT" port="$MC_PORT" waited="${PORT_CLOSE_WAIT_MAX}")"
      fi
    else
      safe_to_force=1
    fi
  else
    log "拿不到公网 IP，无法确认 Minecraft 端口已关闭；本次禁止强制断电"
  fi

  log "提交普通关机请求(停止计费)"
  if ! "$TCCLI" cvm StopInstances \
        --InstanceIds "[\"$INSTANCE_ID\"]" \
        --StopType SOFT \
        --StoppedMode STOP_CHARGING \
        --region "$REGION" --output json >/dev/null 2>&1; then
    notify "$(msg ALERT_STOP_API_FAILED log_file="$LOG_FILE")"
    return 1
  fi

  # 提交成功 ≠ 已停止计费, 必须确认状态真的翻到 STOPPED
  local deadline=$((SECONDS + STATE_WAIT_MAX))
  while [ "$SECONDS" -lt "$deadline" ]; do
    state=$(get_state)
    [ "$state" = "STOPPED" ] && { log "已 STOPPED, 计费已停止"; record_op stop; return 0; }
    sleep 5
  done

  # 普通关机失败后只在 Minecraft 端口已经关闭时强制断电。端口还开着意味着
  # manager 可能没有保存完成，宁可继续计费并告警，也不冒世界损坏的风险。
  if [ "$safe_to_force" != 1 ]; then
    notify "$(msg ALERT_STOP_STILL_RUNNING state="$state")"
    return 1
  fi

  log "普通关机后仍为 $state，提交强制断电请求"
  if ! "$TCCLI" cvm StopInstances \
    --InstanceIds "[\"$INSTANCE_ID\"]" \
    --StopType HARD \
    --StoppedMode STOP_CHARGING \
    --region "$REGION" --output json >/dev/null 2>&1; then
    notify "$(msg ALERT_STOP_API_FAILED log_file="$LOG_FILE")"
    return 1
  fi

  local force_deadline=$((SECONDS + FORCE_STATE_WAIT_MAX))
  while [ "$SECONDS" -lt "$force_deadline" ]; do
    state=$(get_state)
    if [ "$state" = "STOPPED" ]; then
      log "强制断电后已 STOPPED, 计费已停止"
      record_op stop
      return 0
    fi
    sleep 5
  done

  notify "$(msg ALERT_STOP_STILL_RUNNING state="$state")"
  return 1
}

# ── 值守检查 ──────────────────────────────────────────────────────
# 只读 + 告警, 不改任何东西 —— 纯读, 任何时候跑都安全。
# 按 mc.conf 推出的 BOOT_AT–POWEROFF_AT 算出"现在该不该是开着的",
# 再和实际状态比:
#   该关却 RUNNING → 正在烧钱, 最要命的一种
#   该开却 STOPPED → 玩家进不来
# 过渡态(STARTING/STOPPING/REBOOTING 等)一律不插手。
cmd_guard() {
  local state now on_at off_at
  state=$(get_state)
  if [ -z "$state" ]; then
    log "值守检查: 查询实例状态失败(检查 tccli 凭据与网络)"
    return 1
  fi

  now=$(date +%s)
  on_at=$(date -d "$(date +%F) $BOOT_AT" +%s 2>/dev/null)
  off_at=$(date -d "$(date +%F) $POWEROFF_AT" +%s 2>/dev/null)
  if [ -z "$on_at" ] || [ -z "$off_at" ]; then
    log "值守检查: 拿不到开机/断电时刻 ($BOOT_AT / $POWEROFF_AT), mc.conf 缺失或损坏? 本次跳过"
    return 1
  fi

  if [ "$now" -lt $(( on_at - GUARD_GRACE )) ] || [ "$now" -gt $(( off_at + GUARD_GRACE )) ]; then
    # ── 该是关着的时段 ────────────────────────────────────────────
    # 只在确凿的 RUNNING 上告警, 其余状态(含 STOPPING)都算正常, 静默。
    if [ "$state" = "RUNNING" ]; then
      # 这一条带【四个按钮】:
      #   · 关机 —— 最该一键处置的那一种。点一下 = 客户端发一条「关机」回来,
      #     由 QQ 菜单守护进程接住, 和手动发「关机」走同一条路(包括那道
      #     "刚开机不让关"的冷却);
      #   · 忽略 1/2/5h —— 把「多久之内别再吵我」交给值班的人, 见 snoozed()。
      local text
      if snoozed "$SNOOZE_KEY"; then
        log "「正在烧钱」已被忽略到 $(date -d "@$(snooze_until "$SNOOZE_KEY")" '+%F %H:%M'), 这次只记日志(退出码照旧, 仍是问题)"
      else
        text=$(msg ALERT_GUARD_BURNING now="$(date '+%F %H:%M')" \
                    boot="$BOOT_AT" poweroff="$POWEROFF_AT")
        log "告警: $text"
        _push alert "$(msg ALERT_LABEL_BURNING)" "$text" \
              "$(msg BUTTON_STOP):$(msg BUTTON_STOP)" \
              "$(msg BUTTON_IGNORE_1H):$(msg BUTTON_IGNORE_1H)" \
              "$(msg BUTTON_IGNORE_2H):$(msg BUTTON_IGNORE_2H)" \
              "$(msg BUTTON_IGNORE_5H):$(msg BUTTON_IGNORE_5H)"
      fi
      return 1
    fi
    return 0
  fi

  # ── 该是开着的时段 ──────────────────────────────────────────────
  case "$state" in
    RUNNING) return 0 ;;
    STOPPED)
      # 开机窗口刚开始的 GUARD_GRACE 秒内还没起来是正常的: start 提交
      # 请求后还要等端口, 那段时间实例可能还没翻到 RUNNING。
      [ "$now" -lt $(( on_at + GUARD_GRACE )) ] && return 0
      # 和「在烧钱」那条一样四个按钮: 【开机】+ 三个【忽略】。
      #   开机 —— 该开却没开, 手机上最该能做的就是一键让它起来。点一下 =
      #     客户端发一条「开机」回来, 和菜单上那个按钮走完全同一条路
      #     (白名单 + cvm-power.sh start, 起来后回地址);
      #   忽略 —— 今晚就是不开(或者已经知道了), 别每半小时吵一次。
      # 时刻全部从 mc.conf 传进文案 —— 以前这里把 "19:50" 写死在文案里,
      # 改了游玩窗口它就会开始说假话。
      local text
      if snoozed "$SNOOZE_KEY"; then
        log "「该开却没开」已被忽略到 $(date -d "@$(snooze_until "$SNOOZE_KEY")" '+%F %H:%M'), 这次只记日志(退出码照旧, 仍是问题)"
      else
        text=$(msg ALERT_GUARD_SHOULD_BE_UP now="$(date '+%F %H:%M')" \
                    boot="$BOOT_AT" poweroff="$POWEROFF_AT")
        log "告警: $text"
        _push alert "$(msg ALERT_LABEL_NOT_UP)" "$text" \
              "$(msg BUTTON_START):$(msg BUTTON_START)" \
              "$(msg BUTTON_IGNORE_1H):$(msg BUTTON_IGNORE_1H)" \
              "$(msg BUTTON_IGNORE_2H):$(msg BUTTON_IGNORE_2H)" \
              "$(msg BUTTON_IGNORE_5H):$(msg BUTTON_IGNORE_5H)"
      fi
      return 1
      ;;
    *) return 0 ;;   # STARTING / STOPPING / REBOOTING 等过渡态, 不插手
  esac
}

# ── 入口 ──────────────────────────────────────────────────────────
main() {
  if [ ! -x "$TCCLI" ]; then
    echo "找不到 tccli: $TCCLI (装法: pip3 install --user tccli)" >&2
    exit 1
  fi

  case "${1:-}" in
    start|stop)
      # 防止上一次还没跑完又被调度器拉起(比如开机等端口的 5 分钟里)
      exec 200>"$STATE_DIR/.lock" || exit 1
      if ! flock -n 200; then
        echo "另一个 cvm-power 进程正在运行, 本次退出" >&2
        exit 0
      fi
      # --scheduled 只有 Ansible 安装的定时开机单元会带 ——
      # cmd_start 靠它区分"定时任务"和"人点的开机", 后者要能覆盖「取消今晚」。
      SCHEDULED=0
      [ "${2:-}" = "--scheduled" ] && SCHEDULED=1
      cmd_"$1" ;;
    # status / guard 是纯读的, 刻意【不取锁】—— start 最多要占着锁
    # 十分钟等端口, 那段时间里恰恰最需要 guard 能正常跑完并告警。
    status) cmd_status ;;
    guard)  cmd_guard ;;
    # 取消今晚: 只写两个本地文件(取消标记 + 忽略截止), 不碰云 —— 也【不取锁】
    cancel) cmd_cancel ;;
    # snooze 只写一个本地文件、不碰云, 所以也【不取锁】—— 它多半来自 QQ 菜单
    # 上的一次点击, 没道理排在"正在开机"那十分钟后面。
    snooze)
      if [ -z "${2:-}" ] || [ -z "${3:-}" ]; then
        echo "用法: $0 snooze <告警类型> <秒>" >&2
        exit 2
      fi
      case "$3" in
        *[!0-9]*) echo "snooze 的秒数必须是整数: '$3'" >&2; exit 2 ;;
      esac
      log "忽略 $2 到 $(date -d "@$(snooze_set "$2" "$3")" '+%F %H:%M')"
      ;;
    *)
      echo "用法: $0 start [--scheduled]|stop|status|guard|cancel|snooze <类型> <秒>" >&2
      exit 2
      ;;
  esac
}

main "$@"
