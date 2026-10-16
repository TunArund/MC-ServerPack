"""Load and validate the independently deployed Monitor configuration."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml


CONFIG_FILE = Path(os.environ.get("MCSP_CONFIG", "/app/config.yml"))
TIME_RE = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")
PROVIDER_ID_RE = re.compile(r"[A-Za-z0-9-]+")


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class PowerApiConfig:
    provider: str
    instance_id: str
    instance_region: str


@dataclass(frozen=True)
class ScheduleConfig:
    auto_start: bool
    start_at: str | None
    auto_stop: bool
    stop_at: str | None


@dataclass(frozen=True)
class QQTargetConfig:
    kind: str
    identifier: str


@dataclass(frozen=True)
class QQBotConfig:
    targets: tuple[QQTargetConfig, ...]
    admins: tuple[str, ...]


@dataclass(frozen=True)
class MonitorConfig:
    power_api: PowerApiConfig
    schedule: ScheduleConfig
    qqbot: QQBotConfig = QQBotConfig((), ())


def _mapping(value: object, field: str) -> dict:
    if not isinstance(value, dict):
        raise ConfigError(f"{field} 必须是对象")
    return value


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{field} 必须是非空字符串")
    return value.strip()


def _boolean(value: object, field: str, default: bool = False) -> bool:
    if value is None:
        return default
    if type(value) is not bool:
        raise ConfigError(f"{field} 必须是 true 或 false")
    return value


def _clock(value: object, field: str) -> str:
    if type(value) is int and 0 <= value < 1440:
        value = f"{value // 60:02d}:{value % 60:02d}"
    if not isinstance(value, str) or not TIME_RE.fullmatch(value):
        raise ConfigError(f"{field} 必须是 HH:MM")
    return value


def _optional_clock(section: dict, enabled: bool, key: str, field: str) -> str | None:
    value = section.get(key)
    if enabled:
        return _clock(value, field)
    return _clock(value, field) if value is not None else None


def load(path: Path = CONFIG_FILE) -> MonitorConfig:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"缺少 {path}；请从 config.yml.example 复制") from exc
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"Monitor 配置读取失败：{exc}") from exc
    root = _mapping(document, "config.yml")
    manager = _mapping(root.get("manager"), "manager")
    power = _mapping(manager.get("power-api"), "manager.power-api")
    provider = _text(power.get("provider"), "manager.power-api.provider")
    if not PROVIDER_ID_RE.fullmatch(provider):
        raise ConfigError("manager.power-api.provider 仅允许大小写字母、数字和减号")
    auto_start = _boolean(manager.get("auto-start"), "manager.auto-start")
    auto_stop = _boolean(manager.get("auto-stop"), "manager.auto-stop")
    start_at = _optional_clock(manager, auto_start, "start-at", "manager.start-at")
    stop_at = _optional_clock(manager, auto_stop, "stop-at", "manager.stop-at")
    if auto_start and auto_stop and start_at == stop_at:
        raise ConfigError("manager.start-at 与 manager.stop-at 不能相同")
    qqbot_raw = root.get("qqbot")
    qqbot = {} if qqbot_raw is None else _mapping(qqbot_raw, "qqbot")
    targets_raw = qqbot.get("targets", [])
    admins_raw = qqbot.get("admins", [])
    if not isinstance(targets_raw, list) or not isinstance(admins_raw, list):
        raise ConfigError("qqbot.targets/admins 必须是数组")
    targets: list[QQTargetConfig] = []
    for index, raw in enumerate(targets_raw):
        target = _mapping(raw, f"qqbot.targets[{index}]")
        kind = target.get("type")
        if kind not in ("user", "group"):
            raise ConfigError(f"qqbot.targets[{index}].type 只能是 user 或 group")
        targets.append(QQTargetConfig(
            kind, _text(target.get("id"), f"qqbot.targets[{index}].id"),
        ))
    if len(set(targets)) != len(targets):
        raise ConfigError("qqbot.targets 不能重复")
    admins = tuple(
        _text(value, f"qqbot.admins[{index}]")
        for index, value in enumerate(admins_raw)
    )
    if len(set(admins)) != len(admins):
        raise ConfigError("qqbot.admins 不能重复")
    return MonitorConfig(
        PowerApiConfig(
            provider,
            _text(power.get("instance-id"), "manager.power-api.instance-id"),
            _text(power.get("instance-region"), "manager.power-api.instance-region"),
        ),
        ScheduleConfig(auto_start, start_at, auto_stop, stop_at),
        QQBotConfig(tuple(targets), admins),
    )


def secrets() -> dict[str, str]:
    return {
        "POWER_SECRET_ID": os.environ.get("POWER_SECRET_ID", "").strip(),
        "POWER_SECRET_KEY": os.environ.get("POWER_SECRET_KEY", "").strip(),
    }


def qq_credentials() -> tuple[str, str] | None:
    app_id = os.environ.get("QQBOT_SECRET_ID", "").strip()
    app_key = os.environ.get("QQBOT_SECRET_KEY", "").strip()
    if not app_id and not app_key:
        return None
    if not app_id or not app_key:
        raise ConfigError("QQBOT_SECRET_ID 与 QQBOT_SECRET_KEY 必须同时配置")
    return app_id, app_key
