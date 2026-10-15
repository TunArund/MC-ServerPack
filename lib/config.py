"""Load and validate the terminal-owned project configuration."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import yaml
except ModuleNotFoundError:  # Report a project-specific error from ``load``.
    yaml = None


ROOT = Path(__file__).resolve().parents[1]
CONFIG_FILE = ROOT / "config.yml"
TIME_RE = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")
MEMORY_RE = re.compile(r"[1-9]\d*[kKmMgGtT](?:[bB])?")


class ConfigError(RuntimeError):
    """The configuration cannot be used safely."""


@dataclass(frozen=True)
class Endpoint:
    host: str
    user: str
    directory: str


@dataclass(frozen=True)
class ModpackDefaults:
    auto_stop: bool
    stop_at: str
    stop_notify_elast: int
    stop_notify_step: int
    java: tuple[int, ...]
    max_running: int
    overnum_action: str
    memory_limit: str


@dataclass(frozen=True)
class ManagerConfig:
    endpoint: Endpoint
    auto_stop: bool
    stop_at: str | None
    region: str | None
    instance_id: str | None
    modpack: ModpackDefaults


@dataclass(frozen=True)
class MonitorSchedule:
    auto_start: bool
    start_at: str | None
    auto_stop: bool
    stop_at: str | None


@dataclass(frozen=True)
class QQTarget:
    kind: str
    identifier: str


@dataclass(frozen=True)
class MonitorConfig:
    enabled: bool
    endpoint: Endpoint | None
    manager: MonitorSchedule
    targets: tuple[QQTarget, ...]
    admins: tuple[str, ...]


@dataclass(frozen=True)
class CosConfig:
    enabled: bool
    region: str | None
    bucket: str | None
    prefix: str
    expire: int


@dataclass(frozen=True)
class AppConfig:
    manager: ManagerConfig
    monitor: MonitorConfig
    cos: CosConfig


def _mapping(value: object, field: str, *, optional: bool = False) -> dict[str, Any]:
    if value is None and optional:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{field} 必须是对象")
    return value


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{field} 必须是非空字符串")
    return value.strip()


def _boolean(value: object, field: str, *, default: bool | None = None) -> bool:
    if value is None and default is not None:
        return default
    if type(value) is not bool:
        raise ConfigError(f"{field} 必须是 true 或 false")
    return value


def _positive_int(value: object, field: str, *, minimum: int = 1) -> int:
    if type(value) is not int or value < minimum:
        raise ConfigError(f"{field} 必须是不小于 {minimum} 的整数")
    return value


def _clock(value: object, field: str) -> str:
    # PyYAML 1.1 may parse an unquoted 20:00 as the sexagesimal integer 1200.
    if type(value) is int and 0 <= value < 1440:
        value = f"{value // 60:02d}:{value % 60:02d}"
    if not isinstance(value, str) or not TIME_RE.fullmatch(value):
        raise ConfigError(f"{field} 必须是 HH:MM")
    return value


def _optional_clock(section: dict[str, Any], enabled: bool, key: str, field: str) -> str | None:
    value = section.get(key)
    if enabled:
        return _clock(value, field)
    return _clock(value, field) if value is not None else None


def _endpoint(section: dict[str, Any], field: str) -> Endpoint:
    directory = _text(section.get("dir"), f"{field}.dir")
    if not directory.startswith("/"):
        raise ConfigError(f"{field}.dir 必须是绝对路径")
    return Endpoint(
        _text(section.get("host"), f"{field}.host"),
        _text(section.get("user"), f"{field}.user"),
        directory,
    )


def _read_root(path: str | Path) -> dict[str, Any]:
    if yaml is None:
        raise ConfigError("缺少 PyYAML；请先安装项目依赖")
    source = Path(path)
    try:
        document = yaml.safe_load(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"缺少 {source}；请从 config.yml.example 复制") from exc
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"{source} 读取失败：{exc}") from exc

    return _mapping(document, "config.yml")


def runtime_section(path: str | Path, runtime: str) -> dict[str, Any]:
    """Return one runtime subtree unchanged for deployment serialization."""
    if runtime not in ("manager", "monitor"):
        raise ConfigError(f"未知运行时：{runtime}")
    return _mapping(_read_root(path).get(runtime), runtime)


def render_runtime_section(path: str | Path, runtime: str) -> str:
    """Serialize a runtime subtree without sorting or remapping its keys."""
    if yaml is None:
        raise ConfigError("缺少 PyYAML；请先安装项目依赖")
    return yaml.safe_dump(
        runtime_section(path, runtime), allow_unicode=True, sort_keys=False,
    )


def load(path: str | Path = CONFIG_FILE) -> AppConfig:
    """Read *path* and return a validated, typed configuration."""
    root = _read_root(path)
    manager_raw = _mapping(root.get("manager"), "manager")
    manager_endpoint = _endpoint(manager_raw, "manager")
    schedule_raw = _mapping(manager_raw.get("schedule"), "manager.schedule", optional=True)
    poweroff_raw = _mapping(schedule_raw.get("poweroff"), "manager.schedule.poweroff", optional=True)
    manager_auto_stop = _boolean(
        poweroff_raw.get("enabled"), "manager.schedule.poweroff.enabled", default=False,
    )
    manager_stop_at = _optional_clock(
        schedule_raw, manager_auto_stop, "stop-at", "manager.schedule.stop-at",
    )
    region_value = poweroff_raw.get("region")
    instance_value = poweroff_raw.get("instance-id")
    region = _text(region_value, "manager.schedule.poweroff.region") if region_value is not None else None
    instance_id = _text(instance_value, "manager.schedule.poweroff.instance-id") if instance_value is not None else None
    if manager_auto_stop and (not region or not instance_id):
        raise ConfigError("manager.schedule.poweroff.enabled 需要 region 和 instance-id")

    pack_auto_stop = _boolean(schedule_raw.get("auto-stop"), "manager.schedule.auto-stop", default=False)
    if manager_auto_stop and not pack_auto_stop:
        raise ConfigError("manager.schedule.poweroff.enabled 需要 manager.schedule.auto-stop=true")
    pack_stop_at = _optional_clock(schedule_raw, pack_auto_stop, "stop-at", "manager.schedule.stop-at") or "23:00"
    notify_elast = _positive_int(schedule_raw.get("notify-elast", 60), "manager.schedule.notify-elast")
    notify_step = _positive_int(schedule_raw.get("notify-step", 2), "manager.schedule.notify-step", minimum=2)
    java_raw = manager_raw.get("java", [21])
    if (
        not isinstance(java_raw, list)
        or not java_raw
        or any(type(item) is not int or item not in (8, 17, 21) for item in java_raw)
        or len(set(java_raw)) != len(java_raw)
    ):
        raise ConfigError("manager.java 必须是由 8、17、21 组成的不重复数组")
    action = "reject"
    memory = _text(manager_raw.get("mem-limit", "16g"), "manager.mem-limit")
    if not MEMORY_RE.fullmatch(memory):
        raise ConfigError("manager.mem-limit 必须是类似 16g 的内存大小")
    modpack = ModpackDefaults(
        pack_auto_stop, pack_stop_at, notify_elast, notify_step, tuple(java_raw),
        _positive_int(manager_raw.get("max-running", 1), "manager.max-running"),
        action, memory.lower(),
    )
    _positive_int(manager_raw.get("start-timeout", 300), "manager.start-timeout")
    _positive_int(manager_raw.get("stop-timeout", 180), "manager.stop-timeout")

    monitor_raw = _mapping(root.get("monitor"), "monitor", optional=True)
    monitor_enabled = _boolean(monitor_raw.get("enabled"), "monitor.enabled", default=False)
    monitor_endpoint = _endpoint(monitor_raw, "monitor") if monitor_enabled else None
    monitor_manager_raw = _mapping(monitor_raw.get("manager"), "monitor.manager", optional=True)
    auto_start = _boolean(monitor_manager_raw.get("auto-start"), "monitor.manager.auto-start", default=False)
    auto_stop = _boolean(monitor_manager_raw.get("auto-stop"), "monitor.manager.auto-stop", default=False)
    monitor_region = monitor_manager_raw.get("region")
    monitor_instance = monitor_manager_raw.get("instance-id")
    monitor_schedule = MonitorSchedule(
        auto_start,
        _optional_clock(monitor_manager_raw, auto_start, "start-at", "monitor.manager.start-at"),
        auto_stop,
        _optional_clock(monitor_manager_raw, auto_stop, "stop-at", "monitor.manager.stop-at"),
    )
    if monitor_enabled:
        _text(monitor_region, "monitor.manager.region")
        _text(monitor_instance, "monitor.manager.instance-id")
    qqbot = _mapping(monitor_raw.get("qqbot"), "monitor.qqbot", optional=True)
    targets_raw = qqbot.get("targets", [])
    admins_raw = qqbot.get("admins", [])
    if not isinstance(targets_raw, list) or not isinstance(admins_raw, list):
        raise ConfigError("monitor.qqbot.targets/admins 必须是数组")
    targets: list[QQTarget] = []
    for index, item in enumerate(targets_raw):
        target = _mapping(item, f"monitor.qqbot.targets[{index}]")
        kind = target.get("type")
        if kind not in ("user", "group"):
            raise ConfigError(f"monitor.qqbot.targets[{index}].type 只能是 user 或 group")
        targets.append(QQTarget(kind, _text(target.get("id"), f"monitor.qqbot.targets[{index}].id")))
    admins = tuple(_text(value, f"monitor.qqbot.admins[{index}]") for index, value in enumerate(admins_raw))
    monitor = MonitorConfig(monitor_enabled, monitor_endpoint, monitor_schedule, tuple(targets), admins)

    cos_raw = _mapping(root.get("cos"), "cos", optional=True)
    cos_enabled = _boolean(cos_raw.get("enabled"), "cos.enabled", default=False)
    cos = CosConfig(
        cos_enabled,
        _text(cos_raw.get("region"), "cos.region") if cos_enabled else None,
        _text(cos_raw.get("bucket"), "cos.bucket") if cos_enabled else None,
        str(cos_raw.get("prefix", "/minecraft")),
        _positive_int(cos_raw.get("expire", 3600), "cos.expire"),
    )
    return AppConfig(
        ManagerConfig(manager_endpoint, manager_auto_stop, manager_stop_at, region, instance_id, modpack),
        monitor,
        cos,
    )
