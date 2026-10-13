#!/bin/bash
# ══════════════════════════════════════════════════════════════════
#  mc-notify.sh —— 把一条通知发到 QQ 机器人
#
#  用法:   mc-notify.sh [--group] [--button <标签:指令>]... <来源> <正文>
#            mc-notify.sh "⚠️ MC 关服" "服务端未能在 3 分钟内停止…"
#            mc-notify.sh --group "🎮 服务器已开服" "可以进了"
#            mc-notify.sh --button "关机:关机" --button "忽略1h:忽略1h" "🔥 MC 在烧钱" "…"
#  退出码: 0=已发送 或 未配置(视为无需发送)  1=发送失败  2=用法错误
#
#  默认发给【告警目标】(QQBOT_TARGET); --group 发给【群目标】。
#  配置在 notify.conf —— 它由容器入口从 .env 生成，不要手改。
#
#  排版: 【纯文本】。以前按原生 markdown 发(msg_type=2, `## 标题` + `> 脚注`),
#  但这台机器人根本不渲染 markdown —— 2026-09-26 在真机上看见告警里
#  `> VM-0-14-ubuntu` 的 `>` 原样漏出来了。既然不渲染, 那套标记就只剩下
#  "把符号漏给用户"这一个效果, 所以去掉了, 强调改用 emoji。
#
#  ⚠️ 唯一还会用到 msg_type=2 的地方是 --button: 键盘【只能挂在 markdown
#     消息上】, 纯文本带键盘会被平台静默丢掉(2xx, 但按钮不显示)。
#     那时正文仍然是同一份纯文本, markdown 纯粹当载体。
#     --button 可以给多个, 全部排在【一行】里(平台上限一行 5 个)。
#     按下去 = 客户端把「指令」当一条消息发回来, 由 QQ 菜单守护进程接住 ——
#     所以指令必须是那个进程认得的词(COMMANDS 里有)。
#
#  为什么要这个脚本而不是"填个 webhook 地址": QQ 机器人不是静态 webhook ——
#  要先拿 AppID+Secret 换 access_token(约 100 分钟有效), 再带着它调发送接口。
#  这段逻辑集中在这里。token 缓存在 /tmp, 免得每次告警都换一次(平台对换 token
#  有频率限制, 错误码 100001)。
#
#  ⚠️ 消息正文【不能含 URL】: 官方要求消息里出现链接必须先在开放平台后台
#  「开发设置 → 消息 URL 配置」登记, 否则整条发送失败。本脚本会把正文里的
#  http(s) 链接换成占位符兜底。
# ══════════════════════════════════════════════════════════════════
set -uo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF="${NOTIFY_CONF:-$SELF_DIR/notify.conf}"

# shellcheck source=/dev/null
[ -r "$CONF" ] && . "$CONF"

QQBOT_ENABLED="${QQBOT_ENABLED:-0}"
QQBOT_TARGET_TYPE="${QQBOT_TARGET_TYPE:-user}"
TOKEN_API="${TOKEN_API:-https://bots.qq.com/app/getAppAccessToken}"
API_BASE="${API_BASE:-https://api.sgroup.qq.com}"
# token 缓存路径。不能用 /run —— 那是 root 的, Lighthouse 上跑脚本的是普通
# 用户 steam, 写不进去。/tmp 带 sticky 位、谁都能写, 文件本身给 600; 重启
# 就没了, 而 token 本来就只活约 100 分钟。
TOKEN_CACHE="${TOKEN_CACHE:-/tmp/mc-notify-$(id -u).token}"

# ── 用法 ──────────────────────────────────────────────────────────
#   --button <标签:指令> 给这条告警挂一个按钮, 比如 --button "关机:关机"。
#   可以重复给(见文件开头), 顺序就是按钮顺序。
TARGET_MODE="alert"
BUTTONS=()
while [ $# -gt 0 ]; do
  case "${1:-}" in
    --group)  TARGET_MODE="group"; shift ;;
    --button) BUTTONS+=("${2:-}"); shift 2 ;;
    *) break ;;
  esac
done
LABEL="${1:-}"; BODY="${2:-}"
if [ -z "$LABEL" ] || [ -z "$BODY" ]; then
  echo "用法: $0 [--group] [--button <标签:指令>]... <来源> <正文>" >&2
  exit 2
fi

# ── 没配好就静默跳过 ──────────────────────────────────────────────
# 必须返回 0: 告警通道没接不该让关服脚本/看门狗跟着失败。
if [ "$QQBOT_ENABLED" != "1" ]; then
  echo "mc-notify: QQBOT_ENABLED≠1, 跳过" >&2
  exit 0
fi
for v in QQBOT_APPID QQBOT_SECRET; do
  if [ -z "$(eval echo "\${$v:-}")" ]; then
    echo "mc-notify: $v 为空, 跳过(见 $CONF)" >&2
    exit 0
  fi
done

# 解析机器人返回的 JSON 要 python3。缺了要说清楚 —— 否则命令替换里的
# 报错会被 2>/dev/null 吃掉, 表现成"拿到了 token 却解析不出来"。
if ! command -v python3 >/dev/null 2>&1; then
  echo "mc-notify: 找不到 python3, 无法解析机器人返回" >&2
  exit 1
fi

# 目标【没配】时安静跳过(返回 0), 类型写错则响亮失败。
if [ "$TARGET_MODE" = "group" ]; then
  TARGET="${QQBOT_GROUP_TARGET:-}"; TARGET_TYPE="group"
  if [ -z "$TARGET" ]; then
    # 群目标还没配(openid 没抓 / 群主开关没开)—— 退回告警目标。
    # 开服通知发给自己看, 总比谁都没收到好; 群里配好之后自动改走群里。
    TARGET="${QQBOT_TARGET:-}"; TARGET_TYPE="${QQBOT_TARGET_TYPE:-user}"
    [ -n "$TARGET" ] && echo "mc-notify: 没配群目标, 这条改发告警目标" >&2
  fi
  [ -n "$TARGET" ] || { echo "mc-notify: 群目标没配, 告警目标也是空的, 跳过" >&2; exit 0; }
else
  TARGET="${QQBOT_TARGET:-}"; TARGET_TYPE="${QQBOT_TARGET_TYPE:-user}"
  [ -n "$TARGET" ] || { echo "mc-notify: QQBOT_TARGET 为空, 跳过(见 $CONF)" >&2; exit 0; }
fi

case "$TARGET_TYPE" in
  group)   SEND_URL="$API_BASE/v2/groups/$TARGET/messages" ;;
  user)    SEND_URL="$API_BASE/v2/users/$TARGET/messages" ;;
  channel) SEND_URL="$API_BASE/channels/$TARGET/messages" ;;
  *) echo "mc-notify: 目标类型只能是 group/user/channel, 当前 '$TARGET_TYPE'" >&2; exit 1 ;;
esac

# ── 正文兜底 ──────────────────────────────────────────────────────
# 只处理 URL(有链接必须先到后台登记, 否则整条发送失败)。
# 【保留换行】—— markdown 的列表和分段全靠它; 实测 QQ 对 markdown 和纯文本
# 都接受 \n。曾经这里还有一句 tr '\n' ' ', 会把排版压成一坨。
BODY=$(printf '%s' "$BODY" | sed -E 's#https?://[^ ]+#(链接已省略)#g')

# ── 组装正文 ──────────────────────────────────────────────────────
NOW="$(date '+%m-%d %H:%M')"
# 页脚: 发给运维时要带主机名(两台机器, 得知道是谁在喊); 发给玩家群时不带 ——
# 内网主机名对玩家没有意义, 也没必要露出去。
if [ "$TARGET_MODE" = "group" ]; then
  FOOTER="$NOW"
else
  FOOTER="$(hostname) · $NOW"
fi
# 纯文本, 不含任何 markdown 标记 —— 理由见文件开头。
PLAIN="${LABEL}
${BODY}
${FOOTER}"

# ── 取 token(带缓存) ─────────────────────────────────────────────
now=$(date +%s)
token=""; exp=0
if [ -r "$TOKEN_CACHE" ]; then
  read -r token exp _ < "$TOKEN_CACHE" 2>/dev/null || true
fi
# 留 120 秒余量: QQ 在临近过期时会换发新 token
if [ -z "$token" ] || [ "${exp:-0}" -le $(( now + 120 )) ]; then
  resp=$(curl -s -m 15 -X POST "$TOKEN_API" \
    -H 'Content-Type: application/json' \
    -d "{\"appId\":\"$QQBOT_APPID\",\"clientSecret\":\"$QQBOT_SECRET\"}" 2>/dev/null) || {
      echo "mc-notify: 换 token 时网络失败" >&2; exit 1; }

  # 注意: 用 % 而不是 f-string —— f-string 的表达式里不能有反斜杠
  # (Python 3.12 之前), 写成 f"{d.get(\"x\")}" 会直接语法错误, 而
  # 2>/dev/null 会把错误吃掉, 表现成"莫名其妙拿不到 token"。
  parsed=$(printf '%s' "$resp" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
t = d.get("access_token") or ""
e = d.get("expires_in") or 0
try:
    e = int(e)
except Exception:
    e = 0
print("%s %d" % (t, e))
' 2>/dev/null)
  token="${parsed%% *}"
  expires="${parsed##* }"
  if [ -z "$token" ]; then
    # 不能把原始响应整条打出来 —— 成功响应里就含 access_token。
    echo "mc-notify: 换 token 失败, 机器人返回: $(printf '%s' "$resp" | head -c 300 | sed -E 's/"access_token":"[^"]*"/"access_token":"<已隐藏>"/g')" >&2
    exit 1
  fi
  exp=$(( now + ${expires:-0} ))
  # 整块 2>/dev/null: 重定向失败是 bash 报的, 不在 printf 的 stderr 里
  { printf '%s %s\n' "$token" "$exp" > "$TOKEN_CACHE" && chmod 600 "$TOKEN_CACHE"; } 2>/dev/null || true
fi

# ── 发送 ──────────────────────────────────────────────────────────
# 按钮规格摊平成【一行一个 "标签:指令"】交给下面那段 python —— 用换行当分隔,
# 是因为标签和指令里都不会有换行, 而 bash 数组没法直接塞进环境变量。
# 一个按钮都没有时这里就是空串, 键盘整个不加。
BUTTON_SPEC=$(printf '%s\n' ${BUTTONS[@]+"${BUTTONS[@]}"})

# 正文用 python 拼 JSON, 免得引号/反斜杠把请求打散。
#
# $1 = 载体: "0"=纯文本 / "2"=markdown。正文【永远是同一份纯文本】——
# 走 2 只是为了让键盘挂得上去(键盘只认 markdown 消息), 不是为了排版,
# 所以渲染不渲染都长一个样。
build_body() {  # $1=0|2
  CONTENT="$PLAIN" MODE="$1" BUTTON_SPEC="$BUTTON_SPEC" python3 -c '
import json, os
mode = os.environ["MODE"]
body = ({"msg_type": 2, "markdown": {"content": os.environ["CONTENT"]}}
        if mode == "2" else
        {"msg_type": 0, "content": os.environ["CONTENT"]})

# 按钮规格是一行一个 "标签:指令"(理由见上面 BUTTON_SPEC 的拼法)。
# 标签里没有冒号时, 指令就等于标签 —— 那就少写一遍。
buttons = []
for line in (os.environ.get("BUTTON_SPEC") or "").splitlines():
    if not line:
        continue
    label, _, data = line.partition(":")
    label = label or "操作"
    data = data or label
    buttons.append({
        "id": "act%d" % len(buttons),
        "render_data": {"label": label, "visited_label": label, "style": 1},
        "action": {"type": 2, "permission": {"type": 2},
                   "data": data, "enter": True,
                   "unsupport_tips": "请升级 QQ 后使用按钮，或直接发「%s」" % data},
    })
if buttons:
    body["keyboard"] = {"content": {"rows": [{"buttons": buttons}]}}
print(json.dumps(body, ensure_ascii=False))
'
}

try_send() {  # $1=json 正文; 回显平台返回, 成功返回 0
  local out code
  # REPLY 是给调用方读的【全局】, 所以动手之前先清空: 网络就失败时它压根不会被
  # 赋值, 而调用方在 set -u 下引用未定义变量会额外吐一行 "REPLY: unbound
  # variable" —— 那行会把真正的原因("网络失败")淹掉。
  REPLY=""
  out=$(curl -s -m 15 -w '\n@@HTTP=%{http_code}' -X POST "$SEND_URL" \
    -H "Authorization: QQBot $token" \
    -H 'Content-Type: application/json' \
    -d "$1" 2>/dev/null) || { echo "网络失败"; return 1; }
  code=$(printf '%s' "$out" | sed -n 's/.*@@HTTP=\([0-9]*\).*/\1/p')
  REPLY=$(printf '%s' "$out" | sed 's/@@HTTP=[0-9]*//')
  case "$code" in 2*) return 0 ;; *) return 1 ;; esac
}

# 载体只有一个, 没有"失败就换一种"的余地:
#   · 普通告警走纯文本(msg_type=0);
#   · 带按钮的【必须】走 markdown —— 键盘只认它。这里不给退回纯文本的选项,
#     因为退回去等于按钮凭空消失, 而平台会 2xx 收下、不留任何错误 ——
#     2026-09-26 在菜单上正是这么栽的(按钮没了, 日志里一行错都没有)。
MODES="0"
[ -n "$BUTTON_SPEC" ] && MODES="2"

FAILED=""
for m in $MODES; do
  body=$(build_body "$m" 2>/dev/null)
  if [ -z "$body" ]; then FAILED="${FAILED}${FAILED:+ | }拼请求体失败(载体 $m)"; continue; fi
  if try_send "$body"; then
    [ "${MC_NOTIFY_DEBUG:-0}" = "1" ] && \
      echo "mc-notify: 已发送 (msg_type=$m) 到 $SEND_URL" >&2
    exit 0
  fi
  FAILED="${FAILED}${FAILED:+ | }msg_type=$m: $(printf '%s' "$REPLY" | head -c 250)"
done

# 全失败。把平台给的原因原样带出来 —— 「无权限」多半是主动推送开关/权限问题
echo "mc-notify: 发送失败 —— $FAILED" >&2
exit 1
