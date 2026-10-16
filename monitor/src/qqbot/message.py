"""Load and render QQ Bot text templates."""

from __future__ import annotations

from pathlib import Path

import yaml


TEMPLATE_FILE = Path(__file__).with_name("message.yml")


def _load() -> dict[str, str]:
    try:
        value = yaml.safe_load(TEMPLATE_FILE.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise RuntimeError(f"QQ 消息模板读取失败：{exc}") from exc
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(text, str)
        for key, text in value.items()
    ):
        raise RuntimeError("QQ 消息模板必须是字符串对象")
    return value


TEMPLATES = _load()


def render(name: str, **values: object) -> str:
    try:
        template = TEMPLATES[name]
    except KeyError as exc:
        raise RuntimeError(f"缺少 QQ 消息模板：{name}") from exc
    try:
        return template.format(**values)
    except KeyError as exc:
        raise RuntimeError(f"QQ 消息模板 {name} 缺少参数：{exc.args[0]}") from exc
