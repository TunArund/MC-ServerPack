"""加载并校验独立运行的 Monitor 配置。"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import partial
from pathlib import Path

import yaml

from .config_sdk import ConfigError
from .config_sdk import boolean
from .config_sdk import clock as _clock
from .config_sdk import mapping as _mapping
from .config_sdk import text as _text
from .config_sdk import timezone as _timezone

_boolean = partial(boolean, default=False)


CONFIG_FILE = Path(os.environ.get("MCSP_CONFIG", "/app/config.yml"))
PROVIDER_ID_RE = re.compile(r"[A-Za-z0-9_]+")


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
class ManagerApiConfig:
    host: str
    port: int


@dataclass(frozen=True)
class MonitorConfig:
    timezone: str
    power_api: PowerApiConfig
    schedule: ScheduleConfig
    manager_api: ManagerApiConfig


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
    timezone = _timezone(root.get("timezone"), "timezone")
    manager = _mapping(root.get("manager"), "manager")
    power = _mapping(manager.get("power-api"), "manager.power-api")
    provider = _text(power.get("provider"), "manager.power-api.provider")
    if not PROVIDER_ID_RE.fullmatch(provider):
        raise ConfigError("manager.power-api.provider 仅允许大小写字母、数字和下划线")
    auto_start = _boolean(manager.get("auto-start"), "manager.auto-start")
    auto_stop = _boolean(manager.get("auto-stop"), "manager.auto-stop")
    start_at = _optional_clock(manager, auto_start, "start-at", "manager.start-at")
    stop_at = _optional_clock(manager, auto_stop, "stop-at", "manager.stop-at")
    if auto_start and auto_stop and start_at == stop_at:
        raise ConfigError("manager.start-at 与 manager.stop-at 不能相同")
    manager_api = manager.get("api", {})
    if not isinstance(manager_api, dict):
        raise ConfigError("manager.api 必须是对象")
    status_port = manager_api.get("port", 27016)
    if type(status_port) is not int or not 1 <= status_port <= 65535:
        raise ConfigError("manager.api.port 必须是 1 到 65535 的整数")
    status_host = _text(manager_api.get("host"), "manager.api.host")
    return MonitorConfig(
        timezone,
        PowerApiConfig(
            provider,
            _text(power.get("instance-id"), "manager.power-api.instance-id"),
            _text(power.get("instance-region"), "manager.power-api.instance-region"),
        ),
        ScheduleConfig(auto_start, start_at, auto_stop, stop_at),
        ManagerApiConfig(status_host, status_port),
    )


def secrets() -> dict[str, str]:
    return {
        "POWER_SECRET_ID": os.environ.get("POWER_SECRET_ID", "").strip(),
        "POWER_SECRET_KEY": os.environ.get("POWER_SECRET_KEY", "").strip(),
    }


def qq_credentials() -> tuple[str, str] | None:
    app_id = os.environ.get("QQBOT_APP_ID", "").strip()
    app_key = os.environ.get("QQBOT_APP_KEY", "").strip()
    if not app_id and not app_key:
        return None
    if not app_id or not app_key:
        raise ConfigError("QQBOT_APP_ID 与 QQBOT_APP_KEY 必须同时配置")
    return app_id, app_key


def manager_api_secret() -> str:
    value = os.environ.get("MANAGER_API_KEY", "").strip()
    if len(value) < 32:
        raise ConfigError("启用 manager.api 时 MANAGER_API_KEY 至少需要 32 个字符")
    return value
