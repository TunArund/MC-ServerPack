"""Load and validate the terminal-owned project configuration."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

try:
    import yaml
except ModuleNotFoundError:  # Report a project-specific error from ``load``.
    yaml = None


ROOT = Path(__file__).resolve().parents[1]
CONFIG_FILE = ROOT / "config.yml"
ENV_FILE = ROOT / ".env"
TIME_RE = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")
MEMORY_RE = re.compile(r"[1-9]\d*[kKmMgGtT](?:[bB])?")
REMOTE_DIR_RE = re.compile(r"/[A-Za-z0-9._/-]+")
PROVIDER_ID_RE = re.compile(r"[A-Za-z0-9-]+")
TARGET_NAME_RE = re.compile(r"[A-Za-z0-9-]+")
ALERT_KINDS = ("check-failed", "state-mismatch", "recovered")


class ConfigError(RuntimeError):
    """The configuration cannot be used safely."""


def load_env(path: Path = ENV_FILE) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise ConfigError(f"缺少 {path}；请从 .env.example 复制") from exc
    values: dict[str, str] = {}
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("\"'")
    return values


@dataclass(frozen=True)
class Endpoint:
    host: str
    user: str
    directory: str


@dataclass(frozen=True)
class PowerApiConfig:
    provider: str
    instance_id: str
    instance_region: str


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
    power_api: PowerApiConfig | None
    modpack: ModpackDefaults


@dataclass(frozen=True)
class MonitorSchedule:
    auto_start: bool
    start_at: str | None
    auto_stop: bool
    stop_at: str | None


@dataclass(frozen=True)
class QQTarget:
    name: str
    kind: str
    identifier: str


@dataclass(frozen=True)
class QQAlertRoute:
    kind: str
    targets: tuple[str, ...]


@dataclass(frozen=True)
class MonitorConfig:
    enabled: bool
    endpoint: Endpoint | None
    manager: MonitorSchedule
    power_api: PowerApiConfig | None
    targets: tuple[QQTarget, ...]
    admins: tuple[str, ...]
    alerts: tuple[QQAlertRoute, ...]


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
    path = PurePosixPath(directory)
    if not path.is_absolute() or ".." in path.parts or not REMOTE_DIR_RE.fullmatch(directory):
        raise ConfigError(f"{field}.dir 必须是安全的绝对路径")
    if len(path.parts) < 3:
        raise ConfigError(f"{field}.dir 不能是系统顶层目录")
    return Endpoint(
        _text(section.get("host"), f"{field}.host"),
        _text(section.get("user"), f"{field}.user"),
        directory,
    )


def _power_api(section: object, field: str, *, required: bool) -> PowerApiConfig | None:
    if section is None and not required:
        return None
    power = _mapping(section, field)
    instance_id = _text(power.get("instance-id"), f"{field}.instance-id")
    region = _text(power.get("instance-region"), f"{field}.instance-region")
    provider = _text(power.get("provider"), f"{field}.provider")
    if not PROVIDER_ID_RE.fullmatch(provider):
        raise ConfigError(f"{field}.provider 仅允许大小写字母、数字和减号")
    return PowerApiConfig(provider, instance_id, region)


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
    manager_auto_stop = _boolean(manager_raw.get("auto-stop"), "manager.auto-stop", default=False)
    manager_stop_at = _optional_clock(manager_raw, manager_auto_stop, "stop-at", "manager.stop-at")
    manager_power = _power_api(
        manager_raw.get("power-api"), "manager.power-api", required=manager_auto_stop,
    )

    modpack_raw = _mapping(manager_raw.get("modpack"), "manager.modpack")
    pack_auto_stop = _boolean(modpack_raw.get("auto-stop"), "manager.modpack.auto-stop", default=False)
    if manager_auto_stop and not pack_auto_stop:
        raise ConfigError("manager.auto-stop 需要 manager.modpack.auto-stop=true")
    pack_stop_at = _optional_clock(modpack_raw, pack_auto_stop, "stop-at", "manager.modpack.stop-at") or "23:00"
    notify_elast = _positive_int(modpack_raw.get("stop-notify-elast", 60), "manager.modpack.stop-notify-elast")
    notify_step = _positive_int(modpack_raw.get("stop-notify-step", 2), "manager.modpack.stop-notify-step", minimum=2)
    java_raw = modpack_raw.get("java", [21])
    if (
        not isinstance(java_raw, list)
        or not java_raw
        or any(type(item) is not int or item not in (8, 17, 21) for item in java_raw)
        or len(set(java_raw)) != len(java_raw)
    ):
        raise ConfigError("manager.modpack.java 必须是由 8、17、21 组成的不重复数组")
    action = "reject"
    memory = _text(modpack_raw.get("mem-limit", "16g"), "manager.modpack.mem-limit")
    if not MEMORY_RE.fullmatch(memory):
        raise ConfigError("manager.modpack.mem-limit 必须是类似 16g 的内存大小")
    modpack = ModpackDefaults(
        pack_auto_stop, pack_stop_at, notify_elast, notify_step, tuple(java_raw),
        _positive_int(modpack_raw.get("max-running", 1), "manager.modpack.max-running"),
        action, memory.lower(),
    )
    _positive_int(modpack_raw.get("start-timeout", 300), "manager.modpack.start-timeout")
    _positive_int(modpack_raw.get("stop-timeout", 180), "manager.modpack.stop-timeout")

    monitor_raw = _mapping(root.get("monitor"), "monitor", optional=True)
    monitor_enabled = _boolean(monitor_raw.get("enabled"), "monitor.enabled", default=False)
    monitor_endpoint = _endpoint(monitor_raw, "monitor") if monitor_enabled else None
    monitor_manager_raw = _mapping(monitor_raw.get("manager"), "monitor.manager", optional=True)
    auto_start = _boolean(monitor_manager_raw.get("auto-start"), "monitor.manager.auto-start", default=False)
    auto_stop = _boolean(monitor_manager_raw.get("auto-stop"), "monitor.manager.auto-stop", default=False)
    monitor_power = _power_api(
        monitor_manager_raw.get("power-api"), "monitor.manager.power-api",
        required=monitor_enabled,
    )
    monitor_schedule = MonitorSchedule(
        auto_start,
        _optional_clock(monitor_manager_raw, auto_start, "start-at", "monitor.manager.start-at"),
        auto_stop,
        _optional_clock(monitor_manager_raw, auto_stop, "stop-at", "monitor.manager.stop-at"),
    )
    qqbot = _mapping(monitor_raw.get("qqbot"), "monitor.qqbot", optional=True)
    targets_raw = qqbot.get("targets", [])
    admins_raw = qqbot.get("admins", [])
    alerts_raw = qqbot.get("alerts", {})
    if (
        not isinstance(targets_raw, list)
        or not isinstance(admins_raw, list)
        or not isinstance(alerts_raw, dict)
    ):
        raise ConfigError(
            "monitor.qqbot.targets/admins 必须是数组，alerts 必须是对象"
        )
    targets: list[QQTarget] = []
    for index, item in enumerate(targets_raw):
        target = _mapping(item, f"monitor.qqbot.targets[{index}]")
        name = _text(target.get("name"), f"monitor.qqbot.targets[{index}].name")
        if not TARGET_NAME_RE.fullmatch(name):
            raise ConfigError(
                f"monitor.qqbot.targets[{index}].name 仅允许大小写字母、数字和减号"
            )
        kind = target.get("type")
        if kind not in ("user", "group"):
            raise ConfigError(f"monitor.qqbot.targets[{index}].type 只能是 user 或 group")
        targets.append(QQTarget(
            name, kind,
            _text(target.get("id"), f"monitor.qqbot.targets[{index}].id"),
        ))
    names = {target.name for target in targets}
    if len(names) != len(targets):
        raise ConfigError("monitor.qqbot.targets.name 不能重复")
    identities = {(target.kind, target.identifier) for target in targets}
    if len(identities) != len(targets):
        raise ConfigError("monitor.qqbot.targets 的 type/id 不能重复")
    admins = tuple(_text(value, f"monitor.qqbot.admins[{index}]") for index, value in enumerate(admins_raw))
    unknown_kinds = set(alerts_raw) - set(ALERT_KINDS)
    if unknown_kinds:
        raise ConfigError(
            f"monitor.qqbot.alerts 包含未知告警类型：{sorted(unknown_kinds)[0]}"
        )
    alerts: list[QQAlertRoute] = []
    for kind in ALERT_KINDS:
        route = alerts_raw.get(kind, [])
        if not isinstance(route, list):
            raise ConfigError(f"monitor.qqbot.alerts.{kind} 必须是目标名称数组")
        route_names = tuple(
            _text(value, f"monitor.qqbot.alerts.{kind}[{index}]")
            for index, value in enumerate(route)
        )
        if len(set(route_names)) != len(route_names):
            raise ConfigError(f"monitor.qqbot.alerts.{kind} 不能包含重复目标")
        unknown_targets = set(route_names) - names
        if unknown_targets:
            raise ConfigError(
                f"monitor.qqbot.alerts.{kind} 引用了未知目标："
                f"{sorted(unknown_targets)[0]}"
            )
        alerts.append(QQAlertRoute(kind, route_names))
    monitor = MonitorConfig(
        monitor_enabled, monitor_endpoint, monitor_schedule, monitor_power,
        tuple(targets), admins, tuple(alerts),
    )

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
        ManagerConfig(manager_endpoint, manager_auto_stop, manager_stop_at, manager_power, modpack),
        monitor,
        cos,
    )
