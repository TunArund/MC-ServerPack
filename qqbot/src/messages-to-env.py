#!/usr/bin/env python3
"""
把 messages.json 摊平成 bash 能直接 source 的 messages.env

为什么要有这一步:
    messages.json 是文案的唯一来源, 但 bash 读 JSON 天然别扭 —— 要么每次
    发一条告警都起一个 python3 进程, 要么在四个脚本里各写一遍解析。
    所以在【部署时】摊平一次, 运行时就零依赖了。CVM 上为了发条告警去起
    python3, 是这个设计要避免的事。

用法（由容器入口调用，一般不用手跑）:
    python3 messages-to-env.py messages.json > messages.env

摊平规则: 嵌套的键用下划线连起来、转大写、加 MSG_ 前缀。
    menu.text              -> MSG_MENU_TEXT
    alert.guard_burning    -> MSG_ALERT_GUARD_BURNING
下划线开头的键(_note 那种)是给人看的注释, 跳过。
"""
import json
import pathlib
import re
import sys

# 输出【必须】是 UTF-8: 文案里全是中文和 emoji, 而 stdout 的编码跟着环境走 ——
# Windows 控制台是 GBK, Linux 上 LANG 没设时是 ASCII, 两种都会在第一条 emoji
# 上抛 UnicodeEncodeError。而生成出来的文件是要被 bash source 的, 编码错了
# 后面整条链路都是乱码。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass


def flatten(obj, prefix, out, path=""):
    for key, val in obj.items():
        if key.startswith("_"):
            continue
        name = prefix + re.sub(r"\W", "_", key).upper()
        where = "%s.%s" % (path, key) if path else key
        if isinstance(val, dict):
            flatten(val, name + "_", out, where)
        elif isinstance(val, str):
            out.append((name, val))
        else:
            # 悄悄跳过的话, 那条文案到运行时才以"空消息"的形式暴露出来
            sys.exit("messages.json: %s 只能是字符串或对象, 实际是 %s"
                     % (where, type(val).__name__))


def sh_quote(s):
    """单引号包裹。内部的单引号要断开-转义-接上, 否则会把整串打死。"""
    return "'" + s.replace("'", "'\\''") + "'"


def main():
    src = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "messages.json")
    try:
        data = json.loads(src.read_text(encoding="utf-8"))
    except FileNotFoundError:
        sys.exit("找不到 %s" % src)
    except json.JSONDecodeError as e:
        sys.exit("%s 不是合法 JSON: %s" % (src, e))

    out = []
    flatten(data, "MSG_", out)

    print("# 由 messages-to-env.py 从 %s 生成 —— 【不要手改】, 改了会被覆盖。" % src.name)
    print("# 要改文案请改 messages.json，然后重建 qqbot 容器。")
    print("# 被 bash source 使用; 占位符替换见 mc-msg.sh。")
    print()
    for name, val in out:
        print("%s=%s" % (name, sh_quote(val)))

    if not out:
        sys.exit("%s 里一条文案都没有 —— 是不是结构写错了?" % src)


if __name__ == "__main__":
    main()
