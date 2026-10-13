#!/usr/bin/env python3
"""
一次性抓取 QQ 机器人的 group_openid / user_openid

为什么需要它: QQ 官方机器人用的 openid 不是 QQ 群号, 也不是 QQ 号 —— 它只
出现在 QQ 推给机器人的事件里。没有事件接收端就拿不到它, 而拿不到它就没法
往群里主动推消息。

这个脚本临时连一次 QQ 的 WebSocket 网关, 把收到的事件里的 openid 打出来。
**抓到就可以不用再跑它了** —— 往后的主动推送只需要 token + openid, 不需要
保持在线。

用法 (在 Lighthouse 上):

    1. 先把机器人拉进目标群 (群设置 → 机器人 → 添加)
    2. python3 qqbot-openid.py            # 它会在那儿等最多 10 分钟
    3. 在群里 @ 一下机器人, 随便说句话
    4. 脚本打印出 openid 后, 把它填进根 .env 的 QQBOT_TARGET

依赖: websockets (pip3 install --user -i https://mirrors.cloud.tencent.com/pypi/simple/ websockets)

⚠️ 抓到的 openid 是对这个机器人而言的标识, 换机器人就变了。
⚠️ 群里 @ 机器人这件事本身不需要"主动推送开关" —— 那个开关只影响机器人
   能不能【主动】说话, 不影响它【听见】别人说话。所以这个脚本可以先用,
   开关的事之后再管。
"""
import asyncio
import json
import os
import pathlib
import sys
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
DEFAULT_CONF = HERE.parents[1] / ".env" if len(HERE.parents) > 1 else HERE / ".env"
CONF = pathlib.Path(os.environ.get("MC_SERVERPACK_ENV", DEFAULT_CONF))

TOKEN_API = "https://bots.qq.com/app/getAppAccessToken"
API_BASE = "https://api.sgroup.qq.com"
# 注意: 官方文档里那套「先 GET /websocket/ 换一个 wss 地址」的流程已经作废 ——
# 现在 GET 它会返回 426 Upgrade Required。这个路径【本身就是】WebSocket 端点,
# 连接时把 token 放进 Authorization 头即可。(2026-09 在 Lighthouse 上实测)
WS_URL = "wss://api.sgroup.qq.com/websocket/"
# GROUP_AND_C2C_EVENT = 1 << 25 —— 群聊和单聊事件的权限位
INTENTS = 1 << 25
# 等多久没等到就放弃(秒)。可用环境变量覆盖, 方便先挂着一个长时间窗口再去操作 QQ。
WAIT_SECONDS = int(os.environ.get("WAIT_SECONDS", "600"))


def read_conf(path):
    """读取根 .env 中 QQ Bot 所需的字段。"""
    out = dict(os.environ)
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return out


def post_json(url, payload, headers=None):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def get_json(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


async def main():
    try:
        import websockets
    except ImportError:
        sys.exit(
            "缺 websockets 库。装:\n"
            "  pip3 install --user -i https://mirrors.cloud.tencent.com/pypi/simple/ websockets"
        )

    conf = read_conf(CONF)
    appid = conf.get("QQBOT_SECRET_ID", "")
    secret = conf.get("QQBOT_SECRET_KEY", "")
    if not appid or not secret:
        sys.exit(f"{CONF} 里缺 QQBOT_SECRET_ID / QQBOT_SECRET_KEY")

    print("① 换 access_token ...")
    tok = post_json(TOKEN_API, {"appId": appid, "clientSecret": secret})
    token = tok.get("access_token")
    if not token:
        sys.exit(f"换 token 失败: {json.dumps(tok, ensure_ascii=False)[:200]}")
    print("   ok")

    print("② 连 WebSocket 网关 ...")
    print(f"   {WS_URL}")
    print(f"③ 等事件, 最多 {WAIT_SECONDS // 60} 分钟。")
    print("   >> 现在去群里 @ 一下机器人 <<\n")

    # websockets 14+ 用 additional_headers, 更早的版本叫 extra_headers
    import inspect
    hdr_kw = ("additional_headers"
              if "additional_headers" in inspect.signature(websockets.connect).parameters
              else "extra_headers")

    async with websockets.connect(
        WS_URL, max_size=2**22, open_timeout=20,
        **{hdr_kw: {"Authorization": f"QQBot {token}"}},
    ) as ws:
        hb_task = None

        async def heartbeat(interval_ms):
            while True:
                await asyncio.sleep(interval_ms / 1000)
                try:
                    await ws.send(json.dumps({"op": 1, "d": None}))
                except Exception:
                    return

        async def wait_events():
            async for raw in ws:
                ev = json.loads(raw)
                op, t, d = ev.get("op"), ev.get("t"), ev.get("d") or {}

                if op == 10:  # HELLO
                    interval = d.get("heartbeat_interval", 40000)
                    nonlocal hb_task
                    hb_task = asyncio.create_task(heartbeat(interval))
                    await ws.send(json.dumps({
                        "op": 2,
                        "d": {"token": f"QQBot {token}", "intents": INTENTS, "shard": [0, 1]},
                    }))
                elif op == 0 and t == "READY":
                    print(f"   ✓ 已连接, 机器人: {d.get('user', {}).get('username', '?')}")
                elif op == 0 and t in ("GROUP_AT_MESSAGE_CREATE", "GROUP_ADD_ROBOT"):
                    gid = d.get("group_openid")
                    if gid:
                        print("\n" + "=" * 58)
                        print(f"  group_openid = {gid}")
                        print("=" * 58)
                        print("\n填进根 .env:  QQBOT_TARGET=\"%s\"\n" % gid)
                        return gid
                elif op == 0 and t == "C2C_MESSAGE_CREATE":
                    uid = (d.get("author") or {}).get("user_openid")
                    if uid:
                        print("\n" + "=" * 58)
                        print(f"  user_openid = {uid}   (单聊, 用 QQBOT_TARGET_TYPE=\"user\")")
                        print("=" * 58)
                        return uid
                elif op == 9:  # 无效 session, 得重连
                    print("   网关要求重连(不常见), 请重跑一次本脚本")
                    return None
                elif op == 0 and t:
                    print(f"   (事件 {t}, 不是我们要的)")

        try:
            gid = await asyncio.wait_for(wait_events(), timeout=WAIT_SECONDS)
        except asyncio.TimeoutError:
            print(f"\n超时 {WAIT_SECONDS}s 没等到群消息。检查: 机器人进群了吗? @ 的是它吗?")
            return None
        finally:
            if hb_task:
                hb_task.cancel()
        return gid


if __name__ == "__main__":
    try:
        result = asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit("\n已中断")
    sys.exit(0 if result else 1)
