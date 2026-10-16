"""QQ Bot Markdown menu, keyboard and response templates."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Message:
    content: str
    markdown: bool = False
    keyboard: dict | None = None


TEMPLATES = {
    "menu": "# MC Server Pack\n\n云主机状态：**{state}**\n\n请选择电源操作：",
    "permission": "只有配置中的管理员可以执行电源操作。",
    "confirm": "关机将直接请求云主机软关机。请在 60 秒内点击「确认关机」。",
    "confirm-expired": "没有待确认的关机请求，请先点击「关机」。",
    "working": "正在{action}，请稍候。",
    "done": "{action}请求已提交，当前状态：{state}",
    "error": "{action}失败：{reason}",
}


def _button(identifier: str, command: str) -> dict:
    return {
        "id": identifier,
        "render_data": {
            "label": command,
            "visited_label": command,
            "style": 1,
        },
        "action": {
            "type": 2,
            "permission": {"type": 2},
            "data": command,
            "enter": True,
            "unsupport_tips": f"请直接发送「{command}」",
        },
    }


def menu_keyboard() -> dict:
    return {
        "content": {
            "rows": [{
                "buttons": [
                    _button("power-start", "开机"),
                    _button("power-stop", "关机"),
                    _button("power-stop-confirm", "确认关机"),
                ],
            }],
        },
    }


def menu(state: str) -> Message:
    return Message(
        TEMPLATES["menu"].format(state=state),
        markdown=True,
        keyboard=menu_keyboard(),
    )


def text(name: str, **values: object) -> Message:
    return Message(TEMPLATES[name].format(**values))
