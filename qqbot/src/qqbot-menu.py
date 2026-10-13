#!/usr/bin/env python3
"""
落幕曲 MC —— QQ 机器人指令菜单 (常驻 Lighthouse)

为什么要有它:
    在它之前, 机器人只会【说话】不会【听话】—— mc-notify.sh 是纯出站的
    HTTP POST, 没有任何入站通道, 想开关机只能 SSH 上 Lighthouse 敲
    cvm-power.sh。这个进程补上"耳朵"。

为什么必须跑在 Lighthouse 而不是 CVM:
    1. 只有这台机器有云 API 凭据(tccli + CAM), 开关机只能从这儿发起;
    2. CVM 一天有 20.5 小时是关的 —— 放它上面, 等于最需要"开机"的时候
       它自己不在。

它【不重写】任何开关机逻辑: 开关机一律 shell out 给现有的 cvm-power.sh;
优雅关服走 TAT（自动化助手）调用 CVM 上的 manager/manager.sh。
本进程只负责"收指令 → 鉴权 → 转发 → 回话"。

关于【被动回复】(这是整个设计的地基):
    带 msg_id 发出去的消息 = 被动回复, 【不需要】群里那个「主动推送」开关,
    也不消耗主动额度。README 里那个 40034105 只出现在主动推送。
    所以菜单在没开开关的群里照样能用。
    限制: 5 分钟内有效, 同一条最多回 5 次, 靠 msg_seq 递增区分。
    而且正文里可以直接写 <@!openid> —— 「@ 某人」这件事本身也【不用】主动推送。

关于按钮:
    用的是 action.type=2「指令按钮」: 点一下 = 客户端发一条 "@机器人 状态"
    的普通消息, 落回消息通道, 由下面的精确匹配接住。
    另一种 type=1「回调按钮」要求 3 秒内响应, 而我们查状态要调 tccli +
    探端口, 必然超时, 所以不用。
    ⚠️ 平台文档写明按钮能力「自定义按钮内邀开通」—— 这个机器人未必有。
    有三级降级: markdown+键盘 → 纯文本+键盘 → 纯文本。功能一点不少, 只是不能点。

用法:
    python3 qqbot-menu.py               # 前台跑(调试看日志用)
    python3 qqbot-menu.py --self-test   # 离线自检, 不联网
    生产上由 Docker Compose 拉起。

依赖: websockets (pip3 install --user websockets)
"""
import asyncio
import base64
import inspect
import json
import os
import pathlib
import re
import shlex
import sys
import time
import urllib.error
import urllib.request
from collections import OrderedDict

# 日志里有中文和 emoji。容器环境很精简, 万一 LANG 没设,
# Python 会拿 ascii 当 stdout 的编码 —— 第一条中文日志就抛 UnicodeEncodeError
# 把进程打死。显式锁成 UTF-8, 免得落到"我这儿跑得好好的, 到机器上就崩"。
# (Python 3.7+, 所以 recon 确认的 3.10 没问题)
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

HERE = pathlib.Path(__file__).resolve().parent
CONF = pathlib.Path(os.environ.get("NOTIFY_CONF", HERE / "notify.conf"))
POWER_DIR = pathlib.Path(os.environ.get("CVM_POWER_DIR", HERE.parent / "cvm-power"))
if not (POWER_DIR / "cvm-power.sh").exists():
    POWER_DIR = HERE
POWER_STATE_DIR = pathlib.Path(os.environ.get("CVM_POWER_STATE_DIR", POWER_DIR))

TOKEN_API = "https://bots.qq.com/app/getAppAccessToken"
API_BASE = "https://api.sgroup.qq.com"
# 注意: 官方文档里那套「先 GET /websocket/ 换一个 wss 地址」的流程已经作废 ——
# 现在 GET 它会返回 426 Upgrade Required。这个路径【本身就是】WebSocket 端点,
# 连接时把 token 放进 Authorization 头即可。(2026-09 在 Lighthouse 上实测)
WS_URL = "wss://api.sgroup.qq.com/websocket/"
# GROUP_AND_C2C_EVENT = 1 << 25 —— 群聊和单聊事件的权限位
INTENTS = 1 << 25

# 指令是【严格精确匹配】的: 只认这几个词, 没有别名也没有数字码。
# 「菜单」之外那些正是按钮点出来的文本, 所以一条匹配规则同时覆盖
# 「手打」和「点按钮」两条路径。
CMD_MENU, CMD_STATUS, CMD_START, CMD_STOP, CMD_LOGS = "菜单", "状态", "开机", "关机", "日志"
# 「玩家列表」挂在「状态」的回复上(那一条只有一个按钮的位置, 见
# players_keyboard)。它和「状态」一个口径: 谁都能查, 群里也能查 ——
# 名字本来进游戏就能看见, 不像「日志」那样含路径和聊天记录。
CMD_PLAYERS = "玩家列表"
# 「取消预订」= 今晚不玩了: 关机(开着的话) + 不再自动开机 + 值守检查静音到
# 今晚结束。只在【私聊菜单】给按钮 —— 它是个运维动作, 玩家群里没它的位置。
CMD_CANCEL = "取消预订"
# 「忽略」三个只在 guard 那两条告警上作为按钮出现(见 cvm-power.sh 的 guard),
# 【不进菜单】—— 菜单是给玩家的, 而这个只有服主用得上。
# 点一下 = 客户端把标签当消息发回来, 所以它们必须和别的指令一样被匹配到。
CMD_IGNORE_1H, CMD_IGNORE_2H, CMD_IGNORE_5H = "忽略1h", "忽略2h", "忽略5h"
# 写进 .alert-snooze 的那个键。【必须】和 cvm-power.sh 里的 SNOOZE_KEY 一致:
# 不一致的话, 点了忽略会回一句"好", 而告警照旧一条不落 —— 静默失效里最坏的
# 一种, 自检里有守卫钉着这两处。
GUARD_SNOOZE_KEY = "guard"
# 指令 -> 安静多少秒。键必须是上面那几个字符串本身: 按钮的 data 就是它。
IGNORE_SECS = {CMD_IGNORE_1H: 3600, CMD_IGNORE_2H: 7200, CMD_IGNORE_5H: 18000}
COMMANDS = (CMD_MENU, CMD_STATUS, CMD_START, CMD_STOP, CMD_LOGS,
            CMD_IGNORE_1H, CMD_IGNORE_2H, CMD_IGNORE_5H, CMD_PLAYERS,
            CMD_CANCEL)

# 「日志」只在私聊给。理由: 里面有服务器的路径、玩家名、聊天记录 ——
# 那是内部信息, 落到玩家群里是泄露, 对玩家也是噪声。
# 群里手打它不会被当成"没听懂", 而是明确引导去私聊。
PRIVATE_ONLY = (CMD_LOGS,)

# 被动回复最多 5 次(msg_seq 上限), 平台规定的
MAX_MSG_SEQ = 5
# 被动回复的有效窗口。平台是 5 分钟, 这里留 60 秒余量: 卡着最后一秒发
# 很容易超时, 那还不如干脆走主动推送那条路。
PASSIVE_TTL = 240

# 去重表容量。平台会重复推送同一条消息 —— 不去重的话, 一次「关机」
# 会被执行两遍。留 500 条够覆盖任何合理的重推窗口。
DEDUP_SIZE = 500

# 开机成功后多久内不准关机。覆盖冷启动(~2 分钟)加玩家进服那一段 ——
# 「刚开起来就被关掉」几乎总是误操作。
# ⚠️ 只挡这一个方向: 关机之后想重开【永远放行】,"误按了关机赶紧开回来"
# 是必须保留的退路。
POWER_COOLDOWN = 300

# 一条消息的字符预算。日志里最长的一行实测有 966 字符, 还有 3 行 ≥600 ——
# 所以 `tail -n 20` 【不是】一个有大小上界的操作, 撞上一串配置 dump 就能把
# 消息撑爆。宁可少给几行并在末尾标注截断, 也不能让整条发不出去。
MAX_CHARS = 1800

LOG_PREFIX = "qqbot-menu"


# ══════════════════════════════════════════════════════════════════
#  日志
# ══════════════════════════════════════════════════════════════════
def log(msg):
    """打一行带时间戳的日志，Docker 会收进容器日志:
         journalctl -u mc-qqbot-menu -f
    """
    print("[%s] %s" % (time.strftime("%F %T"), msg), flush=True)


def warn(msg):
    log("⚠️ " + msg)


# ══════════════════════════════════════════════════════════════════
#  纯逻辑(不碰网络, 好测)
# ══════════════════════════════════════════════════════════════════
_URL_RE = re.compile(r"https?://\S+")


def sanitize(text):
    """把 http(s) 链接换成占位符。

    平台要求消息里出现链接必须先在开放平台后台「消息 URL 配置」登记, 否则
    【整条发送失败】。宁可少一段信息, 也不能让整条消息发不出去 —— 和
    mc-notify.sh 里那个替换是同一个理由, 只是那条路走的是 bash。
    """
    return _URL_RE.sub("(链接已省略)", text or "")


def match_command(text):
    """把一条消息文本映射成指令。

    严格精确匹配 —— 前后空白无所谓, 但【不做】别名、前缀、数字码。
    返回 None 表示"没听懂"。

    为什么这么死板: 用户明确要求「不要模糊解析」。而且群里有玩家,
    宽松匹配迟早会把某句闲聊认成指令。
    """
    return text.strip() if text.strip() in COMMANDS else None


class RecentMsgs:
    """消息去重 + 记住收到时刻(后续反馈要用)。

    两个用途:
      1. 平台会重推同一条消息 —— 不拦的话「关机」会执行两遍;
      2. 被动回复有 5 分钟窗口, 得知道这条消息是什么时候来的。
    """

    def __init__(self, size=DEDUP_SIZE):
        self.size = size
        self._seen = OrderedDict()   # msg_id -> 收到时刻

    def first_time(self, msg_id):
        """第一次见到返回 True; 重复推送返回 False。"""
        if not msg_id:
            return False
        if msg_id in self._seen:
            return False
        self._seen[msg_id] = time.time()
        while len(self._seen) > self.size:
            self._seen.popitem(last=False)
        return True

    def age(self, msg_id):
        """这条消息收到多久了(秒)。没见过返回 None。"""
        t = self._seen.get(msg_id)
        return None if t is None else time.time() - t

    def can_still_reply_passively(self, msg_id):
        a = self.age(msg_id)
        return a is not None and a < PASSIVE_TTL


class SeqCounter:
    """每条 msg_id 一个 msg_seq 计数器。

    平台要求: 同一条消息的多次回复靠 msg_seq 递增区分, 相同的
    msg_id + msg_seq 重复发送会失败。上限 5 次。
    """

    def __init__(self):
        self._n = {}

    def next(self, msg_id):
        n = self._n.get(msg_id, 0) + 1
        self._n[msg_id] = n
        if len(self._n) > DEDUP_SIZE:
            for k in list(self._n)[:DEDUP_SIZE // 2]:
                del self._n[k]
        return n

    def exhausted(self, msg_id):
        return self._n.get(msg_id, 0) >= MAX_MSG_SEQ


def sender_openid(d):
    """从事件里取发送者 openid。

    不同事件/版本字段名不一样(user_openid / member_openid), 都兜一下 ——
    取错的后果是白名单永远匹配不上, 而那种失败看起来像"配置没生效",
    很难查。
    """
    a = d.get("author") or {}
    return (a.get("user_openid") or a.get("member_openid")
            or d.get("group_member_openid") or "")


def is_admin(openid, admins):
    return bool(openid) and openid in admins


# ══════════════════════════════════════════════════════════════════
#  文案: 全部从 messages.json 取
# ══════════════════════════════════════════════════════════════════
class Texts:
    """messages.json 的读取器。

    取不到键或占位符对不上时, 【把键名原样打出来】而不是给空串 ——
    空消息发出去是静默失败, 而看得见的 MSG_XXX 一眼就知道是哪条丢了。
    (和 mc-msg.sh 那边的行为保持一致。)
    """

    def __init__(self, data):
        self.data = data

    def __call__(self, path, **kw):
        node = self.data
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return "MSG_" + path.upper().replace(".", "_")
            node = node[part]
        if not isinstance(node, str):
            return "MSG_" + path.upper().replace(".", "_")
        try:
            return node.format(**kw)
        except (KeyError, IndexError, ValueError):
            # 占位符没给全, 或者文案里写了没转义的字面大括号 —— 两种都别炸,
            # 把原文打出来让人看见问题在哪
            warn("文案 %s 格式化失败(占位符对不上?), 原样使用" % path)
            return node


_MESSAGES = None


def T(path, **kw):
    """取一条文案。首次调用时才去读 messages.json。

    做成懒加载而不是模块级加载: 这样 --self-test 能单独跑, 也不至于让
    "文件还没部署上去"变成 import 期就 sys.exit 的硬失败。
    """
    global _MESSAGES
    if _MESSAGES is None:
        _MESSAGES = load_messages(HERE / "messages.json")
    return _MESSAGES(path, **kw)


def each_message():
    """遍历 messages.json 里所有 (点分键, 文案)。给自检用。"""
    data = json.loads((HERE / "messages.json").read_text(encoding="utf-8"))
    out = []

    def walk(node, prefix):
        for k, v in node.items():
            if k.startswith("_"):        # 下划线开头是给人看的注释
                continue
            key = "%s.%s" % (prefix, k) if prefix else k
            if isinstance(v, dict):
                walk(v, key)
            elif isinstance(v, str):
                out.append((key, v))

    walk(data, "")
    return out


def has_message(path):
    """messages.json 里有没有这个键(且是字符串)。"""
    global _MESSAGES
    if _MESSAGES is None:
        _MESSAGES = load_messages(HERE / "messages.json")
    node = _MESSAGES.data
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return isinstance(node, str)


def load_messages(path):
    try:
        return Texts(json.loads(pathlib.Path(path).read_text(encoding="utf-8")))
    except FileNotFoundError:
        sys.exit("找不到 %s —— 它是文案的唯一来源, 缺了这个进程说不出话" % path)
    except json.JSONDecodeError as e:
        sys.exit("%s 不是合法 JSON: %s" % (path, e))


# ══════════════════════════════════════════════════════════════════
#  日志: 取回来、剥前缀、合并重复、按预算截断
# ══════════════════════════════════════════════════════════════════
# 日志和 FIFO 由 manager 根目录、整合包 ID 推导。仍然只读 latest.log；
# 同目录的 debug.log 可能很大，不能使用通配符。
MC_MANAGER_DIR = os.environ.get(
    "MC_MANAGER_DIR", "/root/MC-ServerPack/manager"
).rstrip("/")
MC_PACK = os.environ.get("MC_PACK", "example")
if not re.fullmatch(r"[A-Za-z0-9._-]+", MC_PACK):
    sys.exit("MC_PACK 只能包含字母、数字、点、下划线和减号")
MC_PACK_DIR = pathlib.PurePosixPath(MC_MANAGER_DIR) / "modpacks" / MC_PACK
CVM_LOG = str(MC_PACK_DIR / "data" / "logs" / "latest.log")
MC_FIFO = str(MC_PACK_DIR / "data" / "stdin.pipe")

# 日志行的形状:
#   [26Sep2026 01:56:18.966] [Server thread/INFO] [com.cupboard.Cupboard/]: 正文
# 实测时间戳+线程/等级+logger 三段占了【每行 64% 的字节】, 正文平均只有 38 字符。
# 剥掉它们体积降到约三分之一 —— 这是能不能塞进一条 QQ 消息的决定性一步。
_HEAD_RE = re.compile(
    r"^\[([^\]]+)\]\s+"              # [26Sep2026 01:56:18.966]
    r"\[([^\]]*)/([A-Z]+)\]\s+"      # [Server thread/INFO]
    r"(?:\[[^\]]*\]\s*:?\s*)?"       # [logger.name]:  (可选, 有的行没有;
                                     #  注意末尾那个冒号在括号【外面】)
    r"(.*)$"
)
_ERR_LEVELS = ("ERROR", "WARN", "FATAL")


def strip_prefix(line):
    """剥掉前缀。返回 (HH:MM:SS 或 '', 等级 或 '', 正文)。

    对不上形状的行(堆栈续行 `\\tat com.xxx` 就是那种)原样返回 ——
    它们没有等级标记, 硬塞一个等级反而会误导。
    """
    m = _HEAD_RE.match(line.rstrip("\n"))
    if not m:
        return "", "", line.rstrip("\n")
    stamp, _thread, level, msg = m.groups()
    hhmmss = stamp.split(" ")[-1].split(".")[0]     # 26Sep2026 01:56:18.966 -> 01:56:18
    return hhmmss, level, msg


def collapse(rows):
    """把重复的行合并成「文案 ×N」。

    实测: 游玩时段 68% 的报错是同三句模板(`moved too quickly!` ×15、
    `compatible enchantment` ×9、`Can't keep up!` ×3)。不合并的话,
    「最近 20 条报错」就是一坨几乎一样的字, 等于没给信息。
    合并后按【首次出现】的顺序排, 时间也取首次 —— 读起来仍是一条时间线。
    """
    order, seen = [], {}
    for t, level, msg in rows:
        key = (level, msg)
        if key in seen:
            seen[key][3] += 1
        else:
            rec = [t, level, msg, 1]
            seen[key] = rec
            order.append(rec)
    return order


def render_rows(rows, limit, mark_level=False):
    """渲染成文本, 并按【字符数】封顶。

    为什么必须封顶: 最长的一行实测有 966 字符, 还有 3 行 ≥600 ——
    `tail -n 20` 根本不是一个有上界的操作。宁可少给几行, 也不能让整条
    消息因为太长发不出去。
    """
    out, used, cut = [], 0, False
    for t, level, msg, n in rows:
        mark = ""
        if mark_level and level:
            mark = "⛔ " if level in ("ERROR", "FATAL") else "⚠️ "
        suffix = "  ×%d" % n if n > 1 else ""
        text = "%s  %s%s%s" % (t or "--:--:--", mark, msg, suffix)
        if used + len(text) + 1 > limit:
            cut = True
            break
        out.append(text)
        used += len(text) + 1
    if cut:
        out.append(T("logs.truncated"))
    return "\n".join(out)


_SECTION_RE = re.compile(r"^@@(\w+)@@$", re.M)


def parse_sections(blob):
    """把 TAT 的回显按 @@名字@@ 切成 {名字: 内容}。"""
    out, cur = {}, None
    for line in (blob or "").splitlines():
        m = _SECTION_RE.match(line.strip())
        if m:
            cur = m.group(1)
            out[cur] = []
        elif cur:
            out[cur].append(line)
    return {k: "\n".join(v).strip("\n") for k, v in out.items()}


# 一次 TAT 拿齐三样东西。
#
# 为什么合并成一次: 每多一次 TAT 往返就多十几秒, 而「状态」是手机上随手点的
# 按钮 —— 分三次调用的话私聊里的状态要等半分钟。
#
# 三个坑(都是实测踩出来的):
#   · 服务端没跑时往 FIFO 写会【永久阻塞】(没有读者), 必须套 timeout;
#   · 盲发看不到回显, 玩家数只能去日志里捞;
#   · 只能看【发 list 之后】新写入的行 —— 否则上一次 list 的旧结果会被当成
#     本次结果, 人数就永远停在某个陈旧值上, 而且看不出来。
_LIST_SCRIPT = shlex.quote('printf "list\\n" > %s' % shlex.quote(MC_FIFO))
_DIAG_SCRIPT = """L=%s
N=$(wc -l < "$L" 2>/dev/null || echo 0)
echo '@@LOG@@'
tail -n %d "$L" 2>/dev/null
echo '@@ERR@@'
grep -E '/(ERROR|WARN|FATAL)\\]' "$L" 2>/dev/null | tail -n %d
echo '@@PLAY@@'
timeout 5 sh -c %s 2>/dev/null
sleep 3
tail -n +$((N+1)) "$L" 2>/dev/null | grep -o 'There are [0-9]* of a max of [0-9]* players online.*' | tail -1
"""


async def _diag_sections(log_n, err_scan):
    """跑一次诊断脚本, 返回 (成功, 分段后的输出)。"""
    ok, out = await tat_run(
        _DIAG_SCRIPT % (shlex.quote(CVM_LOG), log_n, err_scan, _LIST_SCRIPT),
        timeout=90, poll_max=60, poll_every=2,
    )
    if not ok:
        return False, {}
    return True, parse_sections(out)


def parse_players(line):
    """从服务端的 list 回显里抠出 (名单, 上限)。

    回显实测长这样:
        There are 1 of a max of 20 players online: TunArund
        There are 0 of a max of 20 players online:
    对不上的行返回 (None, None) —— 【不能】退化成"没人": 那是把"问不到"
    说成了"没人", 和人数那边的 None/0 是同一个道理。

    末尾那个 $ 是有意的: 冒号后面【只能】是名字, 或者什么都没有。放宽成
    ".*" 的话, 将来服务端改了措辞(比如句尾多个句号), 我们就会把 ". " 当成
    一个玩家名报出去 —— 宁可不认识这行, 也不能编一个名字出来。
    """
    m = re.search(r"There are (\d+) of a max of (\d+) players online(?::\s*(.*))?$",
                  line or "")
    if not m:
        return None, None
    names = [n.strip() for n in (m.group(3) or "").split(",") if n.strip()]
    return names, int(m.group(2))


async def fetch_players():
    """在线名单。返回 (名单, 上限), 问不到时两个都是 None。

    和人数走同一条路(往服务端管道写 list, 再读它的回显), 所以名单和人数
    天然一致; 也正因为只取 list 之后【新写入】的行, 不可能读到上一次的旧
    结果 —— 理由见 _DIAG_SCRIPT 上面那段。
    """
    ok, sec = await _diag_sections(1, 0)
    if not ok:
        return None, None
    return parse_players((sec.get("PLAY") or "").strip())


async def fetch_diagnostics(log_n=20, err_scan=60):
    """一次拿回 (日志行, 报错行, 在线人数)。

    日志行/报错行都是 (时间, 等级, 正文) 三元组。
    在线人数拿不到时是 None —— 【不能】和「0 人」混为一谈。
    """
    ok, sec = await _diag_sections(log_n, err_scan)
    if not ok:
        return None, None, None
    def rows(name):
        return [strip_prefix(l) for l in sec.get(name, "").splitlines()]
    players = None
    m = re.search(r"There are (\d+) of a max of (\d+)", sec.get("PLAY", ""))
    if m:
        players = int(m.group(1))
    return rows("LOG"), rows("ERR"), players


# ══════════════════════════════════════════════════════════════════
#  配置
# ══════════════════════════════════════════════════════════════════
def read_conf(path):
    """notify.conf 是 bash 语法(KEY="value"), 这里只取需要的几个键。"""
    out = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        sys.exit("找不到 %s —— 它由容器入口从 .env 生成" % path)
    return out


class Config:
    def __init__(self, conf):
        self.appid = conf.get("QQBOT_APPID", "")
        self.secret = conf.get("QQBOT_SECRET", "")
        self.target = conf.get("QQBOT_TARGET", "")
        self.group_target = conf.get("QQBOT_GROUP_TARGET", "")
        # 白名单。没配就默认只有告警目标(也就是运维自己)——
        # 开箱即用, 不用先抓一遍 openid。
        raw = conf.get("QQBOT_ADMINS", "").strip() or self.target
        self.admins = set(raw.split())
        self.enabled = conf.get("QQBOT_ENABLED", "0") == "1"

    def check(self):
        if not self.enabled:
            sys.exit("QQBOT_ENABLED≠1, 不启动 (见 %s)" % CONF)
        if not self.appid or not self.secret:
            sys.exit("%s 里缺 QQBOT_APPID / QQBOT_SECRET" % CONF)


# ══════════════════════════════════════════════════════════════════
#  网络: token / 发消息
# ══════════════════════════════════════════════════════════════════
class Bot:
    def __init__(self, cfg):
        self.cfg = cfg
        self._token = ""
        self._token_exp = 0.0

    # ── token ────────────────────────────────────────────────────
    async def token(self):
        """拿 access_token, 带内存缓存(约 100 分钟有效)。

        刷新留 120 秒余量 —— 平台在临近过期时会换发新 token。
        """
        now = time.time()
        if self._token and self._token_exp > now + 120:
            return self._token
        resp = await asyncio.to_thread(self._fetch_token)
        tok = resp.get("access_token") or ""
        if not tok:
            # 不能把原始响应整条打出来 —— 成功响应里就含 access_token。
            raise RuntimeError("换 token 失败: %s" % json.dumps(resp, ensure_ascii=False)[:200])
        self._token = tok
        try:
            self._token_exp = now + int(resp.get("expires_in") or 0)
        except (TypeError, ValueError):
            self._token_exp = now + 3600
        return tok

    def _fetch_token(self):
        req = urllib.request.Request(
            TOKEN_API,
            data=json.dumps({"appId": self.cfg.appid,
                             "clientSecret": self.cfg.secret}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode())

    # ── 发送 ─────────────────────────────────────────────────────
    def _endpoint(self, kind, target):
        if kind == "group":
            return "%s/v2/groups/%s/messages" % (API_BASE, target)
        if kind == "user":
            return "%s/v2/users/%s/messages" % (API_BASE, target)
        raise ValueError("目标类型只能是 group/user, 收到 %r" % kind)

    def _post(self, url, token, body):
        req = urllib.request.Request(
            url,
            data=json.dumps(body, ensure_ascii=False).encode(),
            headers={"Authorization": "QQBot %s" % token,
                     "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return True, r.read().decode()[:400]
        except urllib.error.HTTPError as e:
            try:
                return False, e.read().decode()[:400]
            except Exception:
                return False, "HTTP %s" % e.code
        except Exception as e:
            return False, "%s: %s" % (type(e).__name__, e)

    async def send(self, kind, target, body):
        """发一条消息。返回 (成功, 平台返回)。

        所有出站正文都在这里过一遍链接过滤 —— 这是唯一的收口点,
        多一条发送路径就少一次漏网的机会。
        """
        tok = await self.token()
        url = self._endpoint(kind, target)
        body = dict(body)
        if "content" in body:
            body["content"] = sanitize(body["content"])
        if "markdown" in body:
            body["markdown"] = dict(body["markdown"])
            body["markdown"]["content"] = sanitize(body["markdown"]["content"])
        return await asyncio.to_thread(self._post, url, tok, body)

    async def reply(self, kind, target, msg_id, seq, content,
                    markdown=False, keyboard=None):
        """被动回复: 带上 msg_id 和 msg_seq。

        markdown=True 走 msg_type=2。⚠️ 两个必须记住的点:
          · 要挂按钮就【必须】走这条路 —— 纯文本消息上的 keyboard 会被
            平台静默丢掉(2xx 但不显示), 见 MENU_TEXT 上面那段;
          · 走 markdown 时 content 必须【留空】, 两者不能同时出现。
        """
        if markdown:
            body = {"msg_id": msg_id, "msg_seq": seq,
                    "markdown": {"content": content}, "msg_type": 2}
        else:
            body = {"msg_id": msg_id, "msg_seq": seq,
                    "content": content, "msg_type": 0}
        if keyboard:
            body["keyboard"] = keyboard
        return await self.send(kind, target, body)

    async def push(self, kind, target, content):
        """主动推送(不带 msg_id)。

        ⚠️ 群主动推送各群默认关闭, 需要群主在手机 QQ 里手动打开 ——
        没打开时平台会回 40034105。所以这条路只在被动回复窗口过期后
        才走, 而且失败了要有人知道(见 followup)。
        """
        return await self.send(kind, target, {"content": content, "msg_type": 0})


# ══════════════════════════════════════════════════════════════════
#  菜单 / 按钮
# ══════════════════════════════════════════════════════════════════
# ── 文案风格: 全部纯文字, 不含 markdown 语法 ──────────────────────
# 渲染与否取决于机器人开没开这个能力、客户端版本、场景, 不渲染时用户看到
# 的是满屏 ** 和 ## —— 比不好看更糟。写成不含语法的纯文字, 渲染不渲染都
# 一个样, 不用赌。(2026-09-26 踩过: 状态那条以 msg_type=0 发出却带着
# **加粗**, 星号直接漏给用户了。)
#
# ── 但【菜单】这条要以 markdown 发出 ──────────────────────────────
# 不是为了排版, 是因为【键盘只能挂在 markdown 消息上】。官方原话:
#   「在 markdown 消息的基础上, 支持消息最底部挂载按钮」
# 正文仍然不含任何 markdown 语法, 所以两种情况下长得一样, 只是多了按钮。
#
# ⚠️ 踩过: 想着"纯文本更保险", 把这里改成 msg_type=0 + keyboard ——
#    平台照样 2xx 收下, 但【键盘被静默丢掉】, 用户看到的菜单没有按钮,
#    而日志里一行错都没有。别再把这条改回去, 自检里有守卫钉着它。
# 排版上有个平台限制值得记: 按钮【只能追加在消息末尾】, 官方原话是
# 「支持消息最底部挂载按钮」—— 正文和键盘是两个平行的段, 文字插不到按钮
# 下面去。所以这里把"点按钮"和"发指令"并成一句话, 而不是分成两行:
# 分两行的话, 后一行必然渲染在按钮【上面】, 读起来像是在讲按钮之后的事。
def menu_attempts():
    """菜单的降级顺序: (正文, 按 markdown 发?, 带键盘?)。

    markdown 在这里【不是排版手段, 是按钮的载体】—— 平台只允许把键盘挂
    在 msg_type=2 的消息上, 纯文本带键盘会被静默丢掉(2xx, 无报错)。
    正文本身不含任何 markdown 语法, 所以渲染不渲染都长一个样。

    做成函数而不是常量, 是因为文案现在住在 messages.json 里、得惰性读;
    也为了让自检能钉住"第一级必须同时是 markdown 和带键盘"这条 ——
    这两个条件是绑定的, 拆开任何一个按钮都会消失。
    """
    return (
        (T("menu.text"),  True,  True),    # markdown 挂着键盘 = 唯一能显示按钮的组合
        (T("menu.plain"), False, False),   # 退回纯文字: 按钮没了, 但用法还在
    )


def make_btn(bid, label, data=None):
    """一个指令按钮。

    action.type=2 = 指令按钮: 点一下, 客户端把 data 当一条消息发回来,
    落回消息通道, 由 match_command 精确匹配接住 —— 所以 data 必须是
    COMMANDS 里的词(自检里有一条守卫盯着 messages.json 的 button.*)。

    data 省略时就用 label: 绝大多数按钮上写的就是要发的那个词, 少写一遍
    就少一处能对不上的地方(和 mc-notify.sh 那个 "标签:指令" 一个规矩)。
    """
    data = data or label
    return {
        "id": bid,
        "render_data": {"label": label, "visited_label": label, "style": 1},
        "action": {
            "type": 2,
            "permission": {"type": 2},
            "data": data,
            "enter": True,
            "unsupport_tips": "请升级 QQ 后使用按钮，或直接发「%s」" % data,
        },
    }


def players_keyboard(kind="user"):
    """「状态」回复上挂的那一个按钮。

    只挂一个: 状态那条本身很短, 按钮多了会把正文顶到屏幕外面去。
    kind 用不上 —— 群聊私聊挂的是同一个按钮, 参数只是为了和 build_keyboard
    同一个调用形状(见 reply_ladder 的 keyboard_fn)。

    为什么挂在「状态」上而不是加进菜单: 看到"现在 3 人在线"之后, 下一个想
    知道的就是"都是谁"。菜单那边已经有四个按钮了, 再加就挤了。
    """
    return {"content": {"rows": [{"buttons": [
        make_btn("players", T("button.players")),
    ]}]}}


def build_keyboard(kind="user"):
    """菜单的指令按钮。群聊三个, 私聊多一个「日志」。

    permission.type=2 = 所有人可操作。按钮【本身不做鉴权】—— 白名单在
    守护进程里判。这样「状态」谁都能点, 「开机/关机」点了会被回没权限,
    比把按钮藏起来更好懂。

    群里不出现「日志」按钮: 那几个字本身就说明了它存在, 而点下去只会被
    引导去私聊。与其给一个点不通的按钮, 不如不给 —— 想用的人手打「日志」
    也会被同样引导一次。
    """
    buttons = [
        make_btn("status", CMD_STATUS),
        make_btn("start",  CMD_START),
        make_btn("stop",   CMD_STOP),
    ]
    if kind != "group":
        buttons.append(make_btn("logs", CMD_LOGS))
        # 「取消预订」单独占一行: 它是"今晚不玩了"这种一次性决定, 和上面那排
        # 日常动作不是一类; 一行五个按钮在手机上也会挤成一团。
        return {"content": {"rows": [{"buttons": buttons},
                                     {"buttons": [make_btn("cancel", T("button.cancel"))]}]}}
    return {"content": {"rows": [{"buttons": buttons}]}}


# ══════════════════════════════════════════════════════════════════
#  外部命令: cvm-power.sh / tccli TAT
# ══════════════════════════════════════════════════════════════════
TCCLI = os.path.expanduser("~/.local/bin/tccli")
INSTANCE_ID = os.environ.get("INSTANCE_ID", "ins-3rnx3mue")
REGION = os.environ.get("REGION", "ap-nanjing")


async def run_cmd(args, timeout=60, env=None):
    """跑一条命令, 返回 (rc, 输出)。超时返回 (None, 提示)。

    rc 为 None 专指超时 —— 调用方必须把它和"跑完但失败"区别对待。
    """
    e = dict(os.environ)
    if env:
        e.update(env)
    try:
        p = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, env=e)
    except FileNotFoundError:
        return 127, "找不到命令: %s" % args[0]
    try:
        out, _ = await asyncio.wait_for(p.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            p.kill()
        except ProcessLookupError:
            pass
        return None, "超时 %ss" % timeout
    return p.returncode, (out or b"").decode("utf-8", "replace")


async def cvm_power(*args, timeout=60, env=None):
    return await run_cmd(["bash", str(POWER_DIR / "cvm-power.sh"), *args],
                         timeout=timeout, env=env)


def _state_of(text):
    """从 cvm-power.sh status 的输出里抠实例状态。

    输出形如:  [2026-09-26 01:00:00] 实例 ins-xxx 状态=RUNNING 公网IP=1.2.3.4
    """
    m = re.search(r"状态=([A-Z]+)", text or "")
    return m.group(1) if m else ""


def _port_open_in(text):
    """status 输出里 27015 是否可连。

    必须先判"不可连": "不可连"里并不含子串"可连"(中间隔着"不"), 但把
    否定式写在前面是个便宜的保险 —— 将来文案改成"不可连(还没起来)"
    之类, 也不会被读反。
    """
    t = text or ""
    if "不可连" in t:
        return False
    return "可连" in t


async def instance_state():
    """只问实例状态。返回 'RUNNING' / 'STOPPED' / '' (查不到)。

    ⚠️ 刻意【不用】cvm-power.sh 的返回码判成败: 它撞上 flock 时会
    静默 exit 0("另一个 cvm-power 进程正在运行, 本次退出")。信返回码
    就会把"没执行"当成"执行成功"。状态是唯一可信的判据。
    """
    rc, out = await cvm_power("status", timeout=45)
    if rc is None:
        return ""
    return _state_of(out)


def last_power_op(path=None):
    """读最近一次【成功】的开关机。返回 (动作, 多久之前秒) 或 (None, None)。

    文件由 cvm-power.sh 写 —— 两边共用一份, 所以定时开机
    也管得住。只放在本进程内存里的话, 冷却对定时开机完全无效, 而那是
    每天唯一一次"没人按按钮的开机"。

    path 可传, 是为了让自检能用临时文件真读一遍: 路径或格式对不上就是
    【静默】失效(永远读到"没记录", 冷却形同虚设), 光测策略函数看不出来。
    """
    try:
        op, ts = (path or (POWER_STATE_DIR / "last-power-op")).read_text().split()
        return op, time.time() - int(ts)
    except Exception:
        # 文件不在(从没跑过) / 内容坏了 —— 一律当作"没有记录", 放行。
        # 冷却是个防误触的护栏, 不该因为读不到它就把正常操作也挡了。
        return None, None


def cooldown_block(op, age, cooldown=POWER_COOLDOWN):
    """该不该因为冷却而拒绝关机。返回 None=放行, 否则是给用户看的一句话。

    纯函数(动作和秒数从外面传), 所以自检能直接钉住规则。
    """
    if op != "start" or age is None or age >= cooldown:
        return None
    mins = int(cooldown - age + 59) // 60
    # 分两种情况而不是把时间拼进一个占位符: 中文里数字和汉字之间要留空格、
    # 而"刚"不需要, 硬拼会拼出"服务器 刚才开起来"这种凭空多一个空格的东西。
    if age < 60:
        return T("reply.cooldown_fresh", mins=mins)
    return T("reply.cooldown_minutes", ago=int(age // 60), mins=mins)


# ── TAT: 在 CVM 里跑命令, 不用 SSH ──────────────────────────────
def _tat_run_args(script, timeout):
    """拼 RunCommand 的 argv。

    ⚠️ 区域参数是【小写】--region: 它是 tccli 的全局选项, 和其他参数
    (--InstanceIds 这类动作参数, PascalCase) 不是一个层级的东西。
    写成 --Region 的话 tccli 直接报 "Unknown options: --Region, ap-nanjing",
    而且【整条命令都不会执行】—— 关机会在第一步就失败。
    (2026-09-26 实测踩到, 官方文档示例里写的是大写。)
    拼参数单独抽成函数, 就是为了让自检能钉住它。
    """
    return [TCCLI, "tat", "RunCommand",
            "--InstanceIds", json.dumps([INSTANCE_ID]),
            "--Content", base64.b64encode(script.encode()).decode(),
            "--CommandType", "SHELL",
            "--Timeout", str(timeout),
            "--region", REGION, "--output", "json"]


def _tat_query_args(invocation_id):
    """按 invocation-id 查这次调用的结果。

    ⚠️ 两个坑, 都是 2026-09-26 实测踩到的:
      · RunCommand 返回的是 CommandId + InvocationId, 【没有】InvocationTaskId
        —— 而 DescribeInvocationTasks 要的是任务 ID(invt-xxx)。把 InvocationId
        当任务 ID 传会得到 InvalidParameterValue, 于是【每一次轮询都失败】,
        一路卡到超时。表现是"状态按钮转半天不出结果", 而日志里一行错都没有。
      · 所以得走 Filters 按 invocation-id 查, 不要用 --InvocationTaskIds。
    """
    return [TCCLI, "tat", "DescribeInvocationTasks",
            "--Filters", json.dumps([{"Name": "invocation-id",
                                      "Values": [invocation_id]}]),
            "--region", REGION, "--output", "json"]


def _tat_output(text):
    """解 TAT 的 Output。

    ⚠️ 它是【base64 的】(实测: "aGVsbG8tZnJvbS10YXQK" = "hello-from-tat\\n")。
    不解码的话拿到的是一串 ASCII 乱码, 而后面所有 grep/regex 都会静默匹配不上
    —— 症状是"服务器明明有报错, 日志却查不到", 比报错难查得多。
    validate=True 是为了让"本来就不是 base64"的情况抛出来走回退, 而不是被
    b64decode 当成合法输入解出一堆垃圾。
    """
    text = text or ""
    try:
        return base64.b64decode(text, validate=True).decode("utf-8", "replace")
    except Exception:
        return text


async def tat_run(script, timeout=300, poll_max=420, poll_every=5):
    """用自动化助手在 CVM 上跑一段 shell, 返回 (成功, 输出)。

    为什么是 TAT 而不是 SSH: 不用开端口、不用配密钥、不用动安全组,
    走 agent 的出站连接, 和现有"一切都走 tccli"的路子一致。

    poll_every 是轮询间隔: 开关机那种慢动作 5 秒合适, 但查日志这种"命令
    本身只跑几秒"的, 5 秒的间隔会让每次查询白白多等好几秒 —— 而状态是
    手机上随手点的按钮, 那几秒是能感觉到的。
    """
    rc, out = await run_cmd(_tat_run_args(script, timeout), timeout=60)
    if rc != 0:
        return False, "RunCommand 调用失败: %s" % (out or "")[-400:]

    # RunCommand 只给 InvocationId, 没有任务 ID —— 查询就靠它, 见 _tat_query_args
    invocation_id = _json_field(out, "InvocationId")
    if not invocation_id:
        return False, "RunCommand 没返回 InvocationId: %s" % (out or "")[-400:]

    deadline = time.time() + poll_max
    last, warned = "", False
    while time.time() < deadline:
        await asyncio.sleep(poll_every)
        rc, out = await run_cmd(_tat_query_args(invocation_id), timeout=45)
        if rc != 0:
            # 必须说一声: 查询失败时整个循环只是在空转, 一路转到超时才返回
            # —— 而那看起来跟"命令跑得慢"一模一样。真出过这个事故。
            # 只警告一次, 免得连失十几次刷屏。
            last = "查询失败: %s" % (out or "").strip()[:120]
            if not warned:
                warn("TAT 结果查询失败, 会一直重试到超时: %s" % last)
                warned = True
            continue
        status = _json_field(out, "TaskStatus")
        stdout = _tat_output(_json_field(out, "Output"))
        if status == "SUCCESS":
            code = _json_field(out, "ExitCode")
            if code in ("", 0, "0"):
                return True, stdout
            # 任务本身"成功"、脚本退出码非 0: 命令跑完了, 是脚本自己报的失败。
            # 对关服流程来说这就是"世界没落盘", 必须当成失败 —— 输出要带回去。
            return False, "脚本退出码 %s: %s" % (code, stdout.strip()[:300])
        if status in ("FAILED", "TIMEOUT", "PARTIAL_FAILED"):
            return False, "任务 %s: %s" % (status, (stdout or last).strip()[:300])
        last = status or last
    return False, "等 TAT 任务结果超时(%ss, 最后状态 %s)" % (poll_max, last or "未知")


def _json_field(blob, key):
    """从 tccli 的 JSON 输出里抠一个字段出来, 嵌多深都找。

    tccli 的返回结构会随版本嵌套层数变化(有时在 InvocationTaskSet[0]
    里, 有时直接平铺), 写死路径很容易在某次升级后静默取空。
    """
    try:
        d = json.loads(blob)
    except Exception:
        return ""
    stack = [d]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            if key in cur and not isinstance(cur[key], (dict, list)):
                return cur[key]
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return ""


# ══════════════════════════════════════════════════════════════════
#  三个动作
# ══════════════════════════════════════════════════════════════════
async def mc_conf():
    """读 mc.conf 里推导出来的时刻和地址。返回 dict, 拿不到的就是空串。

    它是 bash 片段(不是 key=value), 所以 source 一下让它自己算 ——
    照着 mc-status.sh 的路子, 不重复实现推导逻辑。
    这些值只用来写提示文案(python 这边不做任何时间判断)。
    """
    script = ('. "%s" 2>/dev/null; '
              'echo "BOOT_AT=$BOOT_AT"; echo "PLAY_START=$PLAY_START"; '
              'echo "PLAY_END=$PLAY_END"; echo "MC_ADDR=$MC_ADDR"') % pathlib.Path(
                  os.environ.get("MC_CONF", POWER_DIR / "mc.conf"))
    rc, out = await run_cmd(["bash", "-c", script], timeout=15)
    vals = {}
    for key in ("BOOT_AT", "PLAY_START", "PLAY_END", "MC_ADDR"):
        m = re.search(r"^%s=(.*)$" % key, out or "", re.M)
        vals[key] = m.group(1).strip() if (rc == 0 and m) else ""
    return vals


async def do_status(kind):
    """状态: 只读, 谁都能查。

    群聊里【不带报错】—— 报错跟日志同属服务器内部信息(路径/玩家名/聊天),
    只在私聊给。玩家在群里要的本来也只是「开着没、几个人」。

    只调【一次】cvm-power.sh status —— 它内部已经做了 tccli 查询 + 端口
    探测。拆成两次会让"状态"变成 20 秒起步, 而这是手机上随手点的一个按钮。

    文案是给玩家看的, 所以不出现"实例""计费""过渡态"这些词 —— 他们只关心
    "现在能不能进"。运维要的细节在 mc-status.sh 里, 那才是给自己看的。
    """
    rc, out = await cvm_power("status", timeout=45)
    if rc is None:
        return T("status.timeout")

    state = _state_of(out)
    conf = await mc_conf()
    port_ok = _port_open_in(out)

    errs = None
    players = None
    if state == "RUNNING" and port_ok:
        # 日志尾巴这次用不上(log_n=1), 但省下的只是几 KB 流量, 省不掉那次
        # TAT 往返 —— 所以还是一起取回来。
        _logs, errs, players = await fetch_diagnostics(log_n=1, err_scan=60)

    if state == "RUNNING":
        if not port_ok:
            head = T("status.booting")
        elif players:                          # None(问不到) 和 0(没人) 要分开
            head = T("status.running", players=players)
        elif players == 0:
            head = T("status.running_nobody")
        else:
            head = T("status.running_no_count")
    elif state == "STOPPED":
        if conf["BOOT_AT"] and conf["PLAY_START"]:
            head = T("status.stopped_window",
                     boot=conf["BOOT_AT"], play_start=conf["PLAY_START"])
        else:
            head = T("status.stopped")
    elif state:
        head = T("status.transitioning")
    else:
        head = T("status.unknown")

    lines = [head]
    if conf["MC_ADDR"]:
        lines.append(T("status.addr", addr=conf["MC_ADDR"]))

    if kind != "group":
        if errs is None:
            pass                # 取不到就整段不提 —— 报一个查不出来的"报错"更糟
        else:
            recent = collapse([r for r in errs if r[1] in _ERR_LEVELS])[-3:]
            lines.append("")
            if recent:
                lines.append(T("logs.errors_header", n=len(recent)))
                lines.append(render_rows(recent, MAX_CHARS // 3, mark_level=True))
            else:
                lines.append(T("logs.no_errors"))

    return "\n".join(lines)


async def do_players():
    """在线名单 —— 「状态」上那个按钮点出来的就是它。

    和「状态」一个口径: 谁都能查、群里也能查。代价是一次 TAT 往返(实测
    十几到几十秒): 服务端没有"查询"接口, 只能往它的控制台管道写一条 list,
    再读它吐回来的那行。所以点了要等一下, 别以为是卡住了。
    """
    names, _max = await fetch_players()
    if names is None:
        return T("players.unknown")
    if not names:
        return T("players.nobody")
    return T("players.list", n=len(names), names="、".join(names))


async def do_logs(log_n=20, err_n=20):
    """日志: 只有私聊能看。返回【消息列表】—— 一条塞不下, 见下。

    先把每行的前缀剥掉再发 —— 时间戳+线程+logger 三段占了实测每行 64% 的
    字节, 不剥的话 20 行就是 2.8 KB, 手机上要滑好几屏, 而且大半是重复的
    "[26Sep2026 01:56:18.966] [Server thread/INFO] [xxx/]: "。

    为什么拆成两条: qq 的消息长度上限没有可靠数字, 只能按保守预算截断。
    挤在一条里的话日志和报错各分一半预算, 20 行只能显示 9 行 —— 而"看日志"
    这个功能的价值就在于看全。拆开之后每条都能用满预算, 代价只是多一条消息。
    """
    logs, errs, _players = await fetch_diagnostics(log_n=log_n, err_scan=err_n * 4)
    if logs is None:
        return [T("logs.read_failed", reason="TAT 没跑通")]

    msgs = []
    if not logs:
        msgs.append(T("logs.header", n=0) + "\n" + T("logs.empty"))
    else:
        msgs.append(T("logs.header", n=min(log_n, len(logs))) + "\n" +
                    render_rows([(t, lv, m, 1) for t, lv, m in logs], MAX_CHARS))

    if errs:
        # 报错取比要显示的多(上面 err_scan 放大了 4 倍), 因为合并之后条数会缩水
        recent = collapse([r for r in errs if r[1] in _ERR_LEVELS])[-err_n:]
        msgs.append(T("logs.errors_header", n=len(recent)) + "\n" +
                    render_rows(recent, MAX_CHARS, mark_level=True))
    else:
        msgs.append(T("logs.no_errors"))

    return msgs


async def do_start():
    """开机。返回 (成功, 给用户看的一句话)。

    ⚠️ cvm-power.sh start 会一直阻塞到 27015 真的可连(最多 600 秒),
    所以必须异步跑, 不能卡住事件循环 —— 否则这 10 分钟里机器人对
    所有消息都失聪。
    """
    state = await instance_state()
    if state == "RUNNING":
        return True, T("start.already_running")
    if state and state != "STOPPED":
        return False, T("start.transitioning")

    # CVM_POWER_QUIET: 让它别自己播报开服通知 —— 那条走的是主动推送,
    # 群里开关没开就发不出去。改由本进程在被动窗口里 @ 你, 能到得了。
    rc, out = await cvm_power("start", timeout=700, env={"CVM_POWER_QUIET": "1"})
    if rc is None:
        return False, T("start.timeout")

    # 成败按【状态】判定, 不信返回码 —— 理由见 instance_state()
    rc2, out2 = await cvm_power("status", timeout=45)
    state = _state_of(out2)
    if state != "RUNNING":
        return False, T("start.failed")
    if _port_open_in(out2):
        # 开好了顺手把地址给上 —— 这是「可以进了」那一刻最需要的信息
        conf = await mc_conf()
        msg = T("start.ok")
        if conf["MC_ADDR"]:
            msg += "\n" + T("status.addr", addr=conf["MC_ADDR"])
        return True, msg
    return False, T("start.port_closed")


async def do_stop():
    """优雅关服 + 断电。返回 (成功, 给用户看的一句话)。

    顺序不能反, 也不能省:
      1. TAT 让 CVM 执行 manager/manager.sh stop，由 manager 请求服务端
         保存世界并在配置的宽限期内退出；
      2. 只有第 1 步成功了才断电。

    第 1 步失败就【中止, 不断电】。宁可多烧几小时电、留到 23:05 那条
    定时断电兜底, 也不冒丢存档的险 —— 这套系统一贯是"拿几分钱换确定性"。

    ⚠️ 「刚开机就关机」的冷却不在这里判 —— 由调用方(App.handle)在发 ack
    之前判, 否则会先说"正在保存世界"再补一句"其实不给你关"。
    """
    state = await instance_state()
    if state == "STOPPED":
        return True, T("stop.already_stopped")
    if state and state != "RUNNING":
        return False, T("stop.transitioning")

    shutdown_cmd = "cd %s && ./manager.sh stop %s" % (
        shlex.quote(MC_MANAGER_DIR), shlex.quote(MC_PACK)
    )
    ok, out = await tat_run(shutdown_cmd, timeout=300, poll_max=360)
    if not ok:
        warn("优雅关服失败, 已中止(不断电): %s" % out)
        # 这一条的文案【不】精简: 它要让人一眼看懂"没断电是故意的", 并知道原因
        detail = (out or "").strip()
        # TAT 的前几行只是执行进度，真正的错误通常在末尾。旧逻辑取
        # 前 160 字会恰好把错误截掉，手机上只看到半条路径。
        if len(detail) > 360:
            detail = "…" + detail[-359:]
        return False, T("stop.shutdown_failed", reason=detail)

    # 容器已在宽限期内优雅退出，这才断电。POWEROFF_WAIT_MAX=0 是必须的 ——
    # 否则离 23:05 不到 15 分钟时, cvm-power.sh 会一直等到 23:05。
    rc, out = await cvm_power("stop", timeout=400, env={"POWEROFF_WAIT_MAX": "0"})
    state = await instance_state()
    if state == "STOPPED":
        return True, T("stop.ok")
    return False, T("stop.poweroff_failed")


async def do_cancel():
    """取消今晚: 关机(开着的话) + 不再自动开机 + 值守检查静音到今晚结束。

    ⚠️ 顺序不能反: 【先确认机器真的关掉了, 再去静音】。反过来的话, 关服失败
    时机器还在计费, 而"正在烧钱"的告警已经被静音 —— 那正是这套系统最怕的
    静默烧钱。所以关不掉就整件事中止, 什么也不动。

    第 2、3 步(取消标记 + 静音)都在 cvm-power.sh cancel 里, 一次调用做完。
    """
    state = await instance_state()
    if state is None:
        return False, T("cancel.query_failed")
    if state == "RUNNING":
        ok, text = await do_stop()
        if not ok:
            return False, T("cancel.stop_failed", reason=text)
    elif state != "STOPPED":
        return False, T("start.transitioning")

    rc, out = await cvm_power("cancel", timeout=30)
    if rc != 0:
        warn("cvm-power cancel 失败(rc=%s): %s" % (rc, (out or "").strip()[:200]))
        return False, T("reply.exec_error", reason=(out or "").strip()[:100])
    already = state == "STOPPED"
    return True, T("cancel.already" if already else "cancel.ok")


# ══════════════════════════════════════════════════════════════════
#  消息处理
# ══════════════════════════════════════════════════════════════════
class App:
    def __init__(self, cfg):
        self.cfg = cfg
        self.bot = Bot(cfg)
        self.seen = RecentMsgs()
        self.seq = SeqCounter()
        # 开关机单飞: 一个动作跑着的时候再来一条只回"正在处理中"。
        # cvm-power.sh 自己有 flock, 但排队等锁会让回复延迟很久,
        # 用户看到的像是没反应。
        self.power_lock = asyncio.Lock()

    # ── 发送辅助 ─────────────────────────────────────────────────
    async def reply_ladder(self, kind, target, msg_id, attempts,
                           keyboard_fn=build_keyboard):
        """按顺序试几种发法, 成功一个就停。

        用途是带按钮那条消息的降级: markdown+按钮 → 纯文字。
        平台连 markdown 都不收时(少见)至少把正文说清楚, 功能一点不少,
        只是得手打指令。

        keyboard_fn 决定"带按钮"那一级挂哪个键盘(菜单那套, 还是「状态」
        上那个「玩家列表」), 正文和降级顺序由调用方给。

        每次尝试都【新取一个 msg_seq】: 平台规定相同的 msg_id+msg_seq
        重复发送会失败, 万一上一次其实已经发出去了(只是响应丢了),
        共用 seq 会把后面的尝试全部挡掉。
        """
        last = ""
        for content, markdown, with_kb in attempts:
            if self.seq.exhausted(msg_id):
                break
            seq = self.seq.next(msg_id)          # 一次尝试只吃一个 seq
            # 走 bot.reply 而不是自己拼 body: msg_type 和 content/markdown
            # 二选一那条规则只在 Bot.reply 里写一遍, 免得两处各自漂移。
            ok, resp = await self.bot.reply(
                kind, target, msg_id, seq, content,
                markdown=markdown,
                keyboard=keyboard_fn(kind) if with_kb else None)
            if ok:
                if not with_kb:
                    warn("按钮没能发出去, 已退回纯文字: %s" % resp[:120])
                return True
            last = resp
        warn("发送失败: %s" % last[:300])
        return False

    async def followup(self, kind, target, msg_id, content):
        """办完之后的反馈。content 可以是字符串, 也可以是【多条】消息的列表。

        多条是给日志那种塞不下的情况用的: 一条里挤两段, 两段都会被预算砍掉
        一半; 拆成两条各自用满预算, 代价只是多一条消息。
        """
        parts = [content] if isinstance(content, str) else list(content)
        ok = True
        for part in parts:
            if not await self._push_back(kind, target, msg_id, part):
                ok = False
        return ok

    async def _push_back(self, kind, target, msg_id, content):
        """发一条反馈。

        优先挂在原消息上做被动回复 —— 【不需要】群主动推送开关。
        5 分钟窗口过了才退回主动推送; 那条路走不通就记日志 + 告警,
        不静默丢。
        """
        if msg_id and self.seen.can_still_reply_passively(msg_id) \
                and not self.seq.exhausted(msg_id):
            ok, resp = await self.bot.reply(kind, target, msg_id,
                                            self.seq.next(msg_id), content)
            if ok:
                return True
            warn("被动跟进失败, 改走主动推送: %s" % resp[:200])

        ok, resp = await self.bot.push(kind, target, content)
        if ok:
            return True
        warn("主动推送也失败(群里「主动推送」开关没开?): %s" % resp[:200])
        # 兜底: 至少让运维知道这件事发生了 —— 手机上看不到就算白做
        await run_cmd(["bash", str(HERE / "mc-notify.sh"), T("alert.label_menu"),
                       content], timeout=30)
        return False

    # ── 入口 ─────────────────────────────────────────────────────
    async def handle(self, scene, d):
        msg_id = d.get("id") or ""
        # 去重: 平台会重推同一条消息。不拦的话「关机」会执行两遍。
        if not self.seen.first_time(msg_id):
            log("重复推送, 忽略 (%s)" % msg_id)
            return

        content = (d.get("content") or "").strip()
        openid = sender_openid(d)
        if scene == "GROUP_AT_MESSAGE_CREATE":
            kind, target = "group", (d.get("group_openid") or "")
        else:
            kind, target = "user", openid

        if not target:
            warn("事件里没有目标(openid?), 丢弃: %s" % json.dumps(d, ensure_ascii=False)[:200])
            return

        cmd = match_command(content)
        # 每条都记发送者 openid —— 白名单没配全时(群里那个 openid 和
        # 私聊的不是一回事), 就靠这行日志把它捞出来。
        log("收到 %s: %r 来自 %s (scene=%s)" % (cmd or "未识别", content, openid, scene))

        if cmd is None:
            await self.bot.reply(kind, target, msg_id, self.seq.next(msg_id),
                                 T("reply.not_understood"))
            return

        if cmd == CMD_MENU:
            await self.reply_ladder(kind, target, msg_id, menu_attempts())
            return

        # ── 日志: 只在私聊, 而且要白名单 ─────────────────────────
        # 两道都要: 私聊是因为群里不适合放服务器内部信息; 白名单是因为
        # 任何人都能给机器人发私聊, 而日志里有路径、玩家名、聊天记录。
        if cmd in PRIVATE_ONLY and kind == "group":
            # 这里【不】回"没听懂" —— 那是引导人该怎么做, 不是一条报错
            await self.bot.reply(kind, target, msg_id, self.seq.next(msg_id),
                                 T("logs.private_only"))
            return

        if cmd == CMD_LOGS:
            if not is_admin(openid, self.cfg.admins):
                warn("未授权: %s 试图看日志" % openid)
                await self.bot.reply(kind, target, msg_id, self.seq.next(msg_id),
                                     T("reply.no_permission"))
                return
            try:
                text = await do_logs()
            except Exception as e:
                warn("日志异常: %r" % e)
                text = T("reply.log_error", reason=e)
            await self.followup(kind, target, msg_id, text)
            return

        if cmd == CMD_STATUS:
            try:
                text = await do_status(kind)
            except Exception as e:
                warn("状态异常: %r" % e)
                text = T("reply.status_error", reason=e)
            # 状态这条挂一个「玩家列表」按钮 —— 看到"3 人在线"之后, 下一个想
            # 知道的就是"都是谁"。按钮【只能挂 markdown 消息】, 所以走那条
            # 降级阶梯(和菜单同一套): 平台不收 markdown 时, 状态本身照样发得
            # 出去, 只是没按钮。
            #
            # 阶梯全失败(连纯文本都没发出去)时仍旧退回 followup —— 它会再试
            # 一次被动回复、再走主动推送, 免得状态就这么没了。这里宁可多发
            # 一次, 也不要"点了没反应"。
            if not await self.reply_ladder(
                    kind, target, msg_id,
                    ((text, True, True), (text, False, False)),
                    keyboard_fn=players_keyboard):
                await self.followup(kind, target, msg_id, text)
            return

        # ── 玩家列表: 谁都能查, 和「状态」一个口径 ───────────────
        if cmd == CMD_PLAYERS:
            try:
                text = await do_players()
            except Exception as e:
                warn("玩家名单异常: %r" % e)
                text = T("reply.exec_error", reason=e)
            await self.followup(kind, target, msg_id, text)
            return

        # ── 开机 / 关机 / 忽略: 要白名单 ─────────────────────────
        if not is_admin(openid, self.cfg.admins):
            warn("未授权: %s 试图执行「%s」" % (openid, cmd))
            await self.bot.reply(kind, target, msg_id, self.seq.next(msg_id),
                                 T("reply.no_permission"))
            return

        # ── 「忽略」: 只让 guard 安静一段, 不碰云 ────────────────
        # 排在 power_lock 【前面】: 它只是一次本地文件写入, 没道理排在
        # "正在开机"那十分钟后面 —— 那会儿恰恰是最需要让它安静的。
        if cmd in IGNORE_SECS:
            secs = IGNORE_SECS[cmd]
            rc, out = await cvm_power("snooze", GUARD_SNOOZE_KEY, str(secs), timeout=20)
            if rc != 0:
                reason = (out or "").strip()[:100]
                warn("「%s」失败(rc=%s): %s" % (cmd, rc, reason))
                await self.bot.reply(kind, target, msg_id, self.seq.next(msg_id),
                                     T("reply.exec_error", reason=reason))
            else:
                log("「%s」: 值守检查静音 %d 秒" % (cmd, secs))
                await self.bot.reply(kind, target, msg_id, self.seq.next(msg_id),
                                     T("reply.ignore_ok", hours=secs // 3600))
            return

        # "先看再拿"在这里是安全的: asyncio 是单线程, 从 locked() 到
        # 下面 async with 之间没有 await, 别的协程插不进来。
        # (真并发下这就是个 TOCTOU, 所以特意写明白。)
        if self.power_lock.locked():
            await self.bot.reply(kind, target, msg_id, self.seq.next(msg_id),
                                 T("reply.busy"))
            return

        async with self.power_lock:
            # 冷却检查必须在 ack 之前: 不能先说"正在保存世界"、再补一句
            # "其实不给你关"。放在锁里也顺带让判断和动作是串行的。
            # 「取消预订」也走这道冷却 —— 它内部就会关机, 刚落 5 分钟的服务器
            # 一样要挡住(误点一下就"今晚取消"的代价太大)。
            if cmd in (CMD_STOP, CMD_CANCEL):
                op, age = last_power_op()
                reason = cooldown_block(op, age)
                if reason:
                    log("冷却中, 拒绝「%s」(上次 %s 是 %.0f 秒前)" % (cmd, op, age or 0))
                    await self.bot.reply(kind, target, msg_id,
                                         self.seq.next(msg_id), reason)
                    return

            await self.bot.reply(kind, target, msg_id, self.seq.next(msg_id),
                                 {CMD_START: T("start.doing"),
                                  CMD_CANCEL: T("cancel.doing")}.get(cmd, T("stop.doing")))
            try:
                ok, text = await (do_start() if cmd == CMD_START
                                  else do_cancel() if cmd == CMD_CANCEL
                                  else do_stop())
            except Exception as e:
                ok, text = False, T("reply.exec_error", reason=e)
                warn("「%s」异常: %r" % (cmd, e))
            if not ok:
                warn("「%s」失败: %s" % (cmd, text))
            await self.followup(kind, target, msg_id, text)


# ══════════════════════════════════════════════════════════════════
#  WebSocket 主循环
# ══════════════════════════════════════════════════════════════════
async def connect_once(app):
    """连一次网关, 直到断开才返回。"""
    import websockets

    token = await app.bot.token()
    # websockets 14+ 用 additional_headers, 更早的版本叫 extra_headers
    hdr_kw = ("additional_headers"
              if "additional_headers" in inspect.signature(websockets.connect).parameters
              else "extra_headers")

    async with websockets.connect(
        WS_URL, max_size=2 ** 22, open_timeout=20,
        **{hdr_kw: {"Authorization": "QQBot %s" % token}},
    ) as ws:
        hb = None

        async def heartbeat(interval_ms):
            while True:
                await asyncio.sleep(interval_ms / 1000)
                try:
                    await ws.send(json.dumps({"op": 1, "d": None}))
                except Exception:
                    return

        try:
            async for raw in ws:
                ev = json.loads(raw)
                op, t, d = ev.get("op"), ev.get("t"), (ev.get("d") or {})

                if op == 10:                       # HELLO
                    interval = d.get("heartbeat_interval", 40000)
                    if hb:
                        hb.cancel()
                    hb = asyncio.create_task(heartbeat(interval))
                    await ws.send(json.dumps({
                        "op": 2,
                        "d": {"token": "QQBot %s" % token, "intents": INTENTS,
                              "shard": [0, 1]},
                    }))
                elif op == 0 and t == "READY":
                    log("已连接, 机器人: %s"
                        % d.get("user", {}).get("username", "?"))
                elif op == 0 and t in ("GROUP_AT_MESSAGE_CREATE", "C2C_MESSAGE_CREATE"):
                    # 必须丢进后台任务: 开机要跑十分钟, 卡在读取循环里
                    # 会让机器人这段时间对所有消息失聪。
                    asyncio.create_task(app.handle(t, d))
                elif op in (7, 9):                 # RECONNECT / INVALID_SESSION
                    log("网关要求重连 (op=%s)" % op)
                    return
        finally:
            if hb:
                hb.cancel()


async def serve(app):
    """重连循环。

    这个进程要常年挂着, 所以断线是常态而不是异常: 网络抖动、平台维护、
    token 过期都会断。退避从 5 秒起、翻倍到最多 5 分钟。
    """
    backoff = 5
    while True:
        try:
            await connect_once(app)
            backoff = 5
        except Exception as e:
            warn("连接断开: %s: %s" % (type(e).__name__, e))
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 300)


# ══════════════════════════════════════════════════════════════════
#  自检: 不联网, 只验纯逻辑
# ══════════════════════════════════════════════════════════════════
def self_test():
    fails = []

    def eq(got, want, what):
        if got != want:
            fails.append("%s: 期望 %r, 实际 %r" % (what, want, got))

    # 指令必须精确匹配 —— 模糊匹配会把群里的闲聊认成指令
    for c in COMMANDS:
        eq(match_command(c), c, "精确匹配 %s" % c)
    eq(match_command("  菜单 "), CMD_MENU, "前后空白要去掉")
    for bad in ("1", "2", "3", "status", "菜单啊", "状态如何", "", "关机吧",
                "菜单 状态", "help", "/菜单"):
        eq(match_command(bad), None, "不该认成指令: %r" % bad)

    # 「忽略」按钮: 指令 -> 秒数 必须一一对上。点 5h 却只安静 1h 这种错在
    # 手机上完全看不出来 —— 两次回的都是同一句话。
    eq(sorted(IGNORE_SECS), sorted([CMD_IGNORE_1H, CMD_IGNORE_2H, CMD_IGNORE_5H]),
       "三个忽略指令都要有秒数")
    eq(IGNORE_SECS[CMD_IGNORE_1H], 3600, "忽略1h = 3600 秒")
    eq(IGNORE_SECS[CMD_IGNORE_2H], 7200, "忽略2h = 7200 秒")
    eq(IGNORE_SECS[CMD_IGNORE_5H], 18000, "忽略5h = 18000 秒")
    for c, s in sorted(IGNORE_SECS.items()):
        eq(s // 3600, int(c[-2]), "回话里的 {hours} 要和标签上那个数一致 (%s)" % c)

    # 【跨文件的暗礁】按钮的文案在 messages.json 里, 而点下去发出的就是这条
    # 文案本身(data=标签) —— 所以它必须恰好是 COMMANDS 里的词。差一个字,
    # 点了就是"没听懂", 而告警那边不会报任何错。
    for k in ("button.start", "button.stop", "button.ignore_1h", "button.ignore_2h",
              "button.ignore_5h", "button.players", "button.cancel"):
        eq(has_message(k), True, "按钮文案 %s 存在(拼错了就只是个按钮标签)" % k)
        eq(match_command(T(k)), T(k), "按钮 %s 的文案必须是指令" % k)

    # 「玩家列表」: 名单是从服务端的 list 回显里抠的 —— 那行文本由服务端
    # 决定。认不出来时必须回"问不到", 【不能】退化成"没人"。
    eq(parse_players("There are 1 of a max of 20 players online: TunArund"),
       (["TunArund"], 20), "实测的那行回显")
    eq(parse_players("There are 3 of a max of 20 players online: A, B, C"),
       (["A", "B", "C"], 20), "多个名字按逗号拆")
    eq(parse_players("There are 0 of a max of 20 players online:"),
       ([], 20), "没人时是空名单, 不是 None")
    eq(parse_players("There are 0 of a max of 20 players online"),
       ([], 20), "没有冒号也算")
    eq(parse_players("[26Sep2026 13:59:58.971] [Server thread/INFO]: "
                     "There are 2 of a max of 20 players online: A, B"),
       (["A", "B"], 20), "带日志前缀的行也抠得出来")
    eq(parse_players("There are 0 of a max of 20 players online. (换措辞了)"),
       (None, None), "措辞变了 → 问不到, 不许编个名字出来")
    eq(parse_players(""), (None, None), "空字符串不炸")
    eq(parse_players(None), (None, None), "None 不炸")

    # 「状态」上那个按钮: 就一个, 点下去发的必须是菜单认得的词
    rows = players_keyboard()["content"]["rows"]
    eq(len(rows), 1, "玩家列表键盘只有一行")
    eq([b["action"]["data"] for b in rows[0]["buttons"]], [CMD_PLAYERS],
       "状态上那个按钮点下去发的就是「玩家列表」")

    # 链接必须换成占位符 —— 平台要求消息里的 URL 先登记, 否则【整条发送失败】
    eq(sanitize("看 https://example.com/x 这里"), "看 (链接已省略) 这里", "换掉链接")
    eq(sanitize("http://a.b/c 和 https://d.e/f"), "(链接已省略) 和 (链接已省略)",
       "多个链接都换")
    eq(sanitize("没有链接"), "没有链接", "没链接就别动它")
    eq(sanitize(None), "", "None 不能炸")

    # 【文案里不许有 markdown 语法】。
    # 踩过(2026-09-26): 状态那条是以 msg_type=0(纯文本)发出去的, 正文里却
    # 写着 **加粗** —— 用户看到的是满屏星号, 还以为是机器人坏了。
    # 纯文本就老老实实纯文本, 强调靠 emoji。
    # 只扫含中文的行, 这样能排掉 2 ** 22 这种指数和 **{...} 这种解包。
    offenders = []
    for key, tmpl in each_message():
        for mark in ("**", "##", "~~", "]("):
            if mark in tmpl:
                offenders.append("%s 里有 %r" % (key, mark))
    eq(offenders, [], "文案里不能有 markdown 语法")

    # 源码里每一处 T("x.y") 都得在 messages.json 里存在。拼错了不报错, 只会
    # 静默把 "MSG_X_Y" 当文案发出去 —— 用户看得见, 但完全看不懂。
    # 逐行剥掉注释再扫 —— 不然注释里举的例子 T("x.y") 也会被当成真调用,
    # 于是"这条文案不存在"的假警报把真问题淹了。
    src = (HERE / "qqbot-menu.py").read_text(encoding="utf-8")
    used = set()
    for line in src.splitlines():
        used |= set(re.findall(r'T\(\s*"([A-Za-z_][\w.]*)"', line.split("#", 1)[0]))
    for key in sorted(used):
        eq(has_message(key), True, "文案键 %s 存在" % key)

    # 【跨文件的另一半】bash 那些脚本引用的键(msg BUTTON_START 这种)也在这份
    # messages.json 里, 但上面那轮扫描看不见它们 —— bash 侧的拼写错误同样不
    # 报错, 只会把字面的 "MSG_BUTTON_START" 当成按钮标签发给用户看。
    # 依据是 messages.env（容器启动时生成的摊平结果）：没有它
    # 就不查 —— 单跑自检时可能还没生成, 少查一次好过查错。
    env_file = HERE / "messages.env"
    if env_file.exists():
        have = set(re.findall(r"^MSG_([A-Z0-9_]+)=",
                              env_file.read_text(encoding="utf-8"), re.M))
        missing = set()
        for sh in sorted(HERE.glob("*.sh")):
            for line in sh.read_text(encoding="utf-8").splitlines():
                for key in re.findall(r"\bmsg\s+([A-Z][A-Z0-9_]+)", line.split("#", 1)[0]):
                    if key not in have:
                        missing.add("%s 引用的 %s" % (sh.name, key))
        eq(sorted(missing), [], "bash 里 msg 引用的文案键都存在")

    # 「忽略」写进 .alert-snooze 的键, 【必须】和 cvm-power.sh 里读的那个一致。
    # 不一致时点了忽略会回一句"好", 而告警照旧一条不落 —— 静默失效里最坏的
    # 一种(用户以为已经静音了)。
    sh_src = (POWER_DIR / "cvm-power.sh").read_text(encoding="utf-8")
    m = re.search(r'^SNOOZE_KEY="([^"]+)"', sh_src, re.M)
    eq(m.group(1) if m else None, GUARD_SNOOZE_KEY,
       "菜单写的忽略键和 cvm-power.sh 读的是同一个")

    # 每条文案都要能"给全占位符就格式化成功": 字面量大括号忘了转义的话,
    # 会在真发出去那一刻才炸, 而且只炸那一条。
    for key, tmpl in each_message():
        ph = set(re.findall(r"\{(\w+)\}", tmpl))
        try:
            tmpl.format(**{p: "X" for p in ph})
        except Exception as e:
            fails.append("文案 %s 格式化失败: %s" % (key, e))

    # ── 日志处理: 样本抄的是真机的行 ─────────────────────────────
    eq(strip_prefix("[26Sep2026 01:56:18.971] [Server thread/WARN] [ModernFix/]: "
                    "Dedicated server took 87.475 seconds to load"),
       ("01:56:18", "WARN", "Dedicated server took 87.475 seconds to load"),
       "剥掉三层前缀(实测它占每行 64% 的字节)")
    eq(strip_prefix("[26Sep2026 01:56:18.011] [KubeJS Startup/INFO] [KubeJS Startup/]: "
                    "modifier.js#14: Loaded Java class 'X'"),
       ("01:56:18", "INFO", "modifier.js#14: Loaded Java class 'X'"),
       "logger 里有空格也要剥干净")
    # 堆栈续行没有等级标记 —— 硬塞一个等级反而误导
    eq(strip_prefix("\tat com.Polarice3.Goety.client.particles.X.<clinit>(X.java:1)"),
       ("", "", "\tat com.Polarice3.Goety.client.particles.X.<clinit>(X.java:1)"),
       "对不上形状的行原样保留")

    # 重复合并: 实测游玩时段 68% 的报错是同三句模板
    got = collapse([("01:00:01", "WARN", "moved too quickly!"),
                    ("01:00:02", "WARN", "moved too quickly!"),
                    ("01:00:03", "WARN", "moved too quickly!"),
                    ("01:00:04", "WARN", "Can't keep up!")])
    eq(len(got), 2, "三句重复要合并成一条")
    eq(got[0][3], 3, "合并后要记次数")
    eq(got[0][0], "01:00:01", "时间取首次出现的")

    # 体积封顶: 实测最长一行 966 字符, tail -n 20 不是有界操作
    huge = [("00:00:0%d" % i, "INFO", "x" * 500, 1) for i in range(10)]
    rendered = render_rows(huge, 1200)
    eq(len(rendered) < 1200 + 60, True, "封顶之后不能超预算太多")
    eq(T("logs.truncated") in rendered, True, "截断了要在末尾标注")
    eq(render_rows([("01:00:00", "WARN", "短", 1)], 1200, mark_level=True),
       "01:00:00  ⚠️ 短", "带等级标记的渲染")
    eq(render_rows([("01:00:00", "ERROR", "长", 1)], 1200, mark_level=True),
       "01:00:00  ⛔ 长", "ERROR 用另一个标记")
    eq(render_rows([("01:00:00", "WARN", "短", 1)], 1200),
       "01:00:00  短", "不给标记时就是纯文本")

    # 平台会重推 —— 不拦的话「关机」执行两遍
    seen = RecentMsgs(size=3)
    eq(seen.first_time("m1"), True, "首次")
    eq(seen.first_time("m1"), False, "重复推送要拦住")
    eq(seen.first_time(""), False, "空 id 不算数")
    for i in range(5):
        seen.first_time("x%d" % i)
    eq(seen.first_time("m1"), True, "超出容量后被挤出, 可以重新记")

    # msg_seq 必须递增, 且封顶 5
    sq = SeqCounter()
    eq([sq.next("m") for _ in range(3)], [1, 2, 3], "msg_seq 递增")
    eq(sq.exhausted("m"), False, "3 次还没到顶")
    sq.next("m"); sq.next("m")
    eq(sq.exhausted("m"), True, "到 5 次就该停了")

    # 被动回复窗口
    seen2 = RecentMsgs()
    seen2.first_time("m2")
    eq(seen2.can_still_reply_passively("m2"), True, "刚收到的能被动回")
    eq(seen2.can_still_reply_passively("nope"), False, "没见过的不能")
    seen2._seen["m2"] = time.time() - (PASSIVE_TTL + 10)
    eq(seen2.can_still_reply_passively("m2"), False, "窗口过了要改走主动推送")

    # 发送者 openid: 字段名随事件/版本变, 都得认
    eq(sender_openid({"author": {"user_openid": "u1"}}), "u1", "user_openid")
    eq(sender_openid({"author": {"member_openid": "u2"}}), "u2", "member_openid")
    eq(sender_openid({"group_member_openid": "u3"}), "u3", "group_member_openid")
    eq(sender_openid({}), "", "都没有就是空")

    # 白名单
    eq(is_admin("me", {"me"}), True, "白名单内")
    eq(is_admin("other", {"me"}), False, "白名单外")
    eq(is_admin("", {""}), False, "空 openid 永远不算")

    # tccli 返回结构随版本嵌套层数变, 别写死路径
    eq(_json_field('{"InvocationTaskId":"t1"}', "InvocationTaskId"), "t1", "平铺")
    eq(_json_field('{"A":{"B":[{"TaskStatus":"SUCCESS"}]}}', "TaskStatus"),
       "SUCCESS", "嵌套")
    eq(_json_field("不是 json", "X"), "", "非 JSON 不炸")

    # 拼给 tccli 的参数。写错一个字母的后果是【整条命令不执行】, 而且
    # 报错信息("Unknown options")完全不提是哪一步坏了 —— 所以钉死。
    # (2026-09-26 实测踩过: --Region 写成大写。)
    a = _tat_run_args("echo hi", 300)
    eq("--region" in a, True, "区域必须是小写 --region")
    eq("--Region" in a, False, "大写 --Region 是错的")
    eq(a[a.index("--Content") + 1], base64.b64encode(b"echo hi").decode(),
       "命令体要 base64 编码")
    eq(json.loads(a[a.index("--InstanceIds") + 1]), [INSTANCE_ID],
       "实例 ID 要包成 JSON 数组")
    eq(a[a.index("--Timeout") + 1], "300", "超时要以字符串传")
    q = _tat_query_args("inv-1")
    eq("--region" in q, True, "查询也要小写 region")
    # 踩过(2026-09-26): RunCommand 返回的 InvocationId 被当成任务 ID 传进
    # --InvocationTaskIds, tccli 报 InvalidParameterValue, 于是【每次轮询都
    # 失败】一路转到超时 —— 表现成"状态按钮转半天不出结果", 日志里一行错都没有。
    eq("--InvocationTaskIds" in q, False, "不能按任务 ID 查(RunCommand 不给它)")
    eq(json.loads(q[q.index("--Filters") + 1]),
       [{"Name": "invocation-id", "Values": ["inv-1"]}], "要按 invocation-id 过滤")

    # TAT 的 Output 是 base64 的。不解码的话拿到的是一串 ASCII 乱码, 后面所有
    # grep/regex 都静默匹配不上 —— 症状是"服务器明明有报错, 日志却查不到"。
    eq(_tat_output(base64.b64encode("报错: 世界没落盘\n".encode()).decode()),
       "报错: 世界没落盘\n", "base64 要解开")
    eq(_tat_output(""), "", "空进空出")
    eq(_tat_output("这不是 base64 %$#"), "这不是 base64 %$#",
       "不是 base64 就原样返回, 不能解出一堆垃圾")

    # cvm-power.sh status 的【输出文案】解析。这是最脆的一环: 那边改一个字,
    # 这边就静默失效(状态显示不出来, 而不是报错)。钉死它。
    eq(_state_of("[2026-09-26 01:00:00] 实例 ins-x 状态=RUNNING 公网IP=1.2.3.4"),
       "RUNNING", "抠实例状态")
    eq(_state_of("查询失败: 拿不到实例 ins-x 的状态"), "", "查不到就是空")
    eq(_port_open_in("[x] 实例 ins-x 状态=RUNNING 公网IP=1.2.3.4\n[x] MC 27015 可连"),
       True, "可连")
    eq(_port_open_in("[x] MC 27015 不可连"), False, "不可连")
    eq(_port_open_in("[x] 实例 ins-x 状态=STOPPED"), False,
       "服务端没跑时压根不探端口, 不能当成可连")

    # 白名单的默认值 —— 这是"私聊开箱即用"的保证: 没配就取告警目标,
    # 也就是运维自己。写错了就得先抓一遍 openid 才能用。
    c = Config({"QQBOT_TARGET": "owner1"})
    eq(c.admins, {"owner1"}, "白名单留空时默认取 QQBOT_TARGET")
    c2 = Config({"QQBOT_TARGET": "owner1", "QQBOT_ADMINS": "owner1 grp9"})
    eq(c2.admins, {"owner1", "grp9"}, "显式白名单按空格拆开")
    eq(Config({}).admins, set(), "两个都没配就是空(谁都不能开关机)")

    # 冷却: 【只】挡「刚开机就关机」这一个方向。
    # 反方向必须放行 —— "误按了关机赶紧开回来"是刻意保留的退路。
    eq(cooldown_block("start", 30) is not None, True, "刚开机 30 秒就想关 → 拒绝")
    eq(cooldown_block("start", POWER_COOLDOWN - 1) is not None, True, "差 1 秒也挡")
    eq(cooldown_block("start", POWER_COOLDOWN), None, "到点放行")
    eq(cooldown_block("stop", 5), None, "关机之后想重开 → 永远放行")
    eq(cooldown_block(None, None), None, "没有记录(没跑过 cvm-power) → 放行")
    eq(cooldown_block("start", None), None, "有动作没时间戳 → 放行, 不能炸")

    # last_power_op: 真读一遍 cvm-power.sh 写的那个文件。
    # 光测 cooldown_block 不够 —— 路径或格式对不上时它【静默】失效
    # (永远读到"没记录"), 冷却形同虚设, 而且不报任何错。
    # ⚠️ 必须用临时文件: 部署时会在 Lighthouse 上跑自检, 而守护进程正在
    #    同时运行 —— 往真路径写会污染它正在用的冷却状态。
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        f = pathlib.Path(td) / "last-power-op"
        f.write_text("start %d\n" % int(time.time() - 30))
        op, age = last_power_op(f)
        eq(op, "start", "读出动作")
        eq(25 <= age <= 35, True, "读出的时间差要对(±5 秒容差)")
        eq(cooldown_block(op, age) is not None, True, "读出来的记录要真能拦住关机")
        f.write_text("stop %d\n" % int(time.time()))
        eq(last_power_op(f)[0], "stop", "关机也读得出")
        f.write_text("坏内容")
        eq(last_power_op(f), (None, None), "文件内容坏了当没记录, 不能炸")
        eq(last_power_op(pathlib.Path(td) / "不存在"), (None, None),
           "文件不存在当没记录, 不能炸")

    # 两边靠【文件名】对齐: cvm-power.sh 写、这边读。哪边改名都会静默失效。
    eq("last-power-op" in (POWER_DIR / "cvm-power.sh").read_text(encoding="utf-8"),
       True, "cvm-power.sh 里要有 last-power-op 这个文件名")

    # 键盘必须挂在消息上, 且三个按钮 data 要能被 match_command 认回来
    kb = build_keyboard("user")
    datas = [b["action"]["data"]
             for row in kb["content"]["rows"] for b in row["buttons"]]
    eq(datas, [CMD_STATUS, CMD_START, CMD_STOP, CMD_LOGS, CMD_CANCEL],
       "私聊五个按钮的 data")
    for d in datas:
        eq(match_command(d), d, "按钮点出来的 %s 要能被识别" % d)

    # 【菜单第一级必须同时是 markdown 和带键盘 —— 这两个条件是绑定的】。
    # 踩过(2026-09-26): 想着"纯文本更保险", 把 markdown 去掉只留 keyboard,
    # 平台照样 2xx 收下, 但【键盘被静默丢掉】—— 用户看到没有按钮的菜单,
    # 而日志里一行错都没有, 只能靠人肉发现。键盘只能挂在 markdown 消息上。
    att = menu_attempts()
    eq(att[0][1], True, "菜单第一级必须按 markdown 发")
    eq(att[0][2], True, "菜单第一级必须带键盘")
    eq(any(a[2] for a in att), True, "至少要有一级能把按钮发出去")
    # 最后一级是没按钮的兜底, 那就不该再写"点下面的按钮"
    eq(att[-1][2], False, "最后一级不该带键盘")
    eq("按钮" in att[-1][0], False, "没按钮的那级不能提「按钮」")

    # 群聊不给「日志」, 私聊才给
    def labels(kind):
        kb = build_keyboard(kind)
        return [b["action"]["data"]
                for row in kb["content"]["rows"] for b in row["buttons"]]
    eq(labels("group"), [CMD_STATUS, CMD_START, CMD_STOP], "群里三个按钮")
    eq(labels("user"), [CMD_STATUS, CMD_START, CMD_STOP, CMD_LOGS, CMD_CANCEL],
       "私聊五个按钮")
    # 「取消预订」是运维动作, 【不进群菜单】—— 群里给了也只会被回"只有服主能操作"
    eq(CMD_CANCEL in labels("group"), False, "取消预订不进群菜单")
    eq(len(build_keyboard("user")["content"]["rows"]), 2, "私聊分两行(取消预订独占一行)")
    eq(len(build_keyboard("group")["content"]["rows"]), 1, "群里就一行")

    if fails:
        print("✗ 自检失败 %d 项:" % len(fails))
        for f in fails:
            print("   - " + f)
        return 1
    print("✓ 自检通过（指令精确匹配 / 去重 / msg_seq / 被动窗口 / 鉴权 / "
          "白名单默认值 / 开关机冷却 / tccli 输出解析 / tccli 参数拼装 / "
          "cvm-power 输出解析 / 冷却记录文件读写 / 键盘按钮分场景 / "
          "文案键存在性 / 文案无 markdown 语法 / 日志前缀剥离 / "
          "报错合并 / 体积封顶）")
    return 0


# ══════════════════════════════════════════════════════════════════
def main():
    if "--self-test" in sys.argv:
        sys.exit(self_test())

    cfg = Config(read_conf(CONF))
    cfg.check()
    log("启动。白名单 %d 人: %s"
        % (len(cfg.admins), ", ".join(sorted(cfg.admins)) or "(空)"))

    app = App(cfg)
    try:
        asyncio.run(serve(app))
    except KeyboardInterrupt:
        sys.exit("已中断")


if __name__ == "__main__":
    main()
