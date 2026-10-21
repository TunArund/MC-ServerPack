"""MCSP 共享配置校验原语。

组件配置模型与默认值留在各运行时；这里仅提供无业务归属的类型校验。
"""

from __future__ import annotations

import re
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


TIME_RE = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")
_MISSING = object()


class ConfigError(RuntimeError):
    """配置无法安全使用。"""


def mapping(
    value: object, field: str, *, optional: bool = False,
) -> dict[str, Any]:
    if value is None and optional:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{field} 必须是对象")
    return value


def text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{field} 必须是非空字符串")
    return value.strip()


def boolean(
    value: object, field: str, default: bool | object = _MISSING,
) -> bool:
    if value is None and default is not _MISSING:
        return bool(default)
    if type(value) is not bool:
        raise ConfigError(f"{field} 必须是 true 或 false")
    return value


def positive_integer(value: object, field: str, *, minimum: int = 1) -> int:
    if type(value) is not int or value < minimum:
        raise ConfigError(f"{field} 必须是不小于 {minimum} 的整数")
    return value


def clock(value: object, field: str) -> str:
    # PyYAML 1.1 会把未加引号的 20:00 解析成六十进制整数 1200。
    if type(value) is int and 0 <= value < 1440:
        value = f"{value // 60:02d}:{value % 60:02d}"
    if not isinstance(value, str) or not TIME_RE.fullmatch(value):
        raise ConfigError(f"{field} 必须是 HH:MM")
    return value


def timezone(value: object, field: str) -> str:
    name = text(value, field)
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"{field} 必须是有效的 IANA 时区，例如 Asia/Shanghai") from exc
    return name
