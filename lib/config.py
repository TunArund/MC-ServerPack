"""
解析根config.yml配置
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .remote import HOST_RE

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
PROVIDER_ID_RE = re.compile(r"[A-Za-z0-9_]+")


class ConfigError(RuntimeError):
    """配置无法安全使用。"""


def initialize(path: str | Path = CONFIG_FILE) -> tuple[tuple[Path, bool], ...]:
    """从示例创建 Terminal 配置与密钥文件，不覆盖已有文件。"""
    config = Path(path)
    targets = (
        (config.with_name(config.name + ".example"), config, 0o644),
        (config.parent / ".env.example", config.parent / ".env", 0o600),
    )
    for source, _, _ in targets:
        if not source.is_file():
            raise ConfigError(f"缺少初始化模板：{source}")

    result: list[tuple[Path, bool]] = []
    for source, target, mode in targets:
        content = source.read_bytes()
        try:
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        except FileExistsError:
            result.append((target, False))
            continue
        except OSError as exc:
            raise ConfigError(f"无法创建 {target}：{exc}") from exc
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(content)
        except OSError as exc:
            target.unlink(missing_ok=True)
            raise ConfigError(f"无法创建 {target}：{exc}") from exc
        result.append((target, True))
    return tuple(result)


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
    timezone: str
    auto_stop: bool
    stop_at: str | None
    power_api: PowerApiConfig | None
    modpack: ModpackDefaults
    api_host: str
    api_port: int


@dataclass(frozen=True)
class MonitorSchedule:
    auto_start: bool
    start_at: str | None
    auto_stop: bool
    stop_at: str | None


@dataclass(frozen=True)
class MonitorConfig:
    enabled: bool
    endpoint: Endpoint | None
    timezone: str
    manager: MonitorSchedule
    power_api: PowerApiConfig | None


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


def _host(value: object, field: str) -> str:
    host = _text(value, field)
    if not HOST_RE.fullmatch(host):
        raise ConfigError(f"{field} 必须是有效的 IPv4 地址或域名")
    return host


def _user(value: object, field: str) -> str:
    user = _text(value, f"{field}.user")
    if user == "root":
        raise ConfigError(
            f"{field}.user 不能是 root；请填写普通用户名并使用 "
            f"deploy {field} --create-user 创建"
        )
    return user


def _timezone(value: object, field: str) -> str:
    name = _text(value, field)
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"{field} 必须是有效的 IANA 时区，例如 Asia/Shanghai") from exc
    return name


def _endpoint(section: dict[str, Any], field: str) -> Endpoint:
    directory = _text(section.get("dir"), f"{field}.dir")
    path = PurePosixPath(directory)
    if not path.is_absolute() or ".." in path.parts or not REMOTE_DIR_RE.fullmatch(directory):
        raise ConfigError(f"{field}.dir 必须是安全的绝对路径")
    if len(path.parts) < 3:
        raise ConfigError(f"{field}.dir 不能是系统顶层目录")
    host = _host(section.get("host"), f"{field}.host")
    user = _user(section.get("user"), field)
    return Endpoint(host, user, directory)


def _power_api(section: object, field: str, *, required: bool) -> PowerApiConfig | None:
    if section is None and not required:
        return None
    power = _mapping(section, field)
    instance_id = _text(power.get("instance-id"), f"{field}.instance-id")
    region = _text(power.get("instance-region"), f"{field}.instance-region")
    provider = _text(power.get("provider"), f"{field}.provider")
    if not PROVIDER_ID_RE.fullmatch(provider):
        raise ConfigError(f"{field}.provider 仅允许大小写字母、数字和下划线")
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
    """原样返回一个运行时的配置子树，供部署时序列化。

    monitor 段保持自包含，包含与 manager 段重复的地址配置，方便整段复制到
    Monitor 独立部署目录。
    """
    if runtime not in ("manager", "monitor"):
        raise ConfigError(f"未知运行时：{runtime}")
    return _mapping(_read_root(path).get(runtime), runtime)


def render_runtime_section(path: str | Path, runtime: str) -> str:
    """不排序、不重映射键，直接序列化运行时配置子树。"""
    if yaml is None:
        raise ConfigError("缺少 PyYAML；请先安装项目依赖")
    return yaml.safe_dump(
        runtime_section(path, runtime), allow_unicode=True, sort_keys=False,
    )


def _parse_manager(root: dict[str, Any]) -> ManagerConfig:
    manager_raw = _mapping(root.get("manager"), "manager")
    manager_endpoint = _endpoint(manager_raw, "manager")
    timezone = _timezone(manager_raw.get("timezone"), "manager.timezone")
    manager_auto_stop = _boolean(manager_raw.get("auto-stop"), "manager.auto-stop", default=False)
    manager_stop_at = _optional_clock(manager_raw, manager_auto_stop, "stop-at", "manager.stop-at")
    manager_power = _power_api(
        manager_raw.get("power-api"), "manager.power-api", required=manager_auto_stop,
    )
    api = _mapping(
        manager_raw.get("api"), "manager.api", optional=True,
    )
    api_host = manager_endpoint.host
    listen = api.get("listen", "0.0.0.0")
    if not isinstance(listen, str) or not re.fullmatch(r"(?:\d{1,3}\.){3}\d{1,3}", listen):
        raise ConfigError("manager.api.listen 必须是 IPv4 地址")
    api_port = api.get("port", 27016)
    if type(api_port) is not int or not 1 <= api_port <= 65535:
        raise ConfigError("manager.api.port 必须是 1 到 65535 的整数")

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

    return ManagerConfig(
        manager_endpoint, timezone, manager_auto_stop, manager_stop_at, manager_power,
        modpack, api_host, api_port,
    )


def _parse_monitor(root: dict[str, Any]) -> MonitorConfig:
    monitor_raw = _mapping(root.get("monitor"), "monitor", optional=True)
    if monitor_raw.get("user") == "root":
        raise ConfigError(
            "monitor.user 不能是 root；请填写普通用户名并使用 "
            "deploy monitor --create-user 创建"
        )
    monitor_enabled = _boolean(monitor_raw.get("enabled"), "monitor.enabled", default=False)
    monitor_endpoint = _endpoint(monitor_raw, "monitor") if monitor_enabled else None
    timezone = _timezone(monitor_raw.get("timezone"), "monitor.timezone")
    monitor_manager_raw = _mapping(monitor_raw.get("manager"), "monitor.manager", optional=True)
    auto_start = _boolean(monitor_manager_raw.get("auto-start"), "monitor.manager.auto-start", default=False)
    auto_stop = _boolean(monitor_manager_raw.get("auto-stop"), "monitor.manager.auto-stop", default=False)
    monitor_power = _power_api(
        monitor_manager_raw.get("power-api"), "monitor.manager.power-api",
        required=monitor_enabled,
    )
    if monitor_enabled:
        monitor_api = _mapping(
            monitor_manager_raw.get("api"), "monitor.manager.api",
        )
        _host(monitor_api.get("host"), "monitor.manager.api.host")
        monitor_port = monitor_api.get("port", 27016)
        if type(monitor_port) is not int or not 1 <= monitor_port <= 65535:
            raise ConfigError("monitor.manager.api.port 必须是 1 到 65535 的整数")
    monitor_schedule = MonitorSchedule(
        auto_start,
        _optional_clock(monitor_manager_raw, auto_start, "start-at", "monitor.manager.start-at"),
        auto_stop,
        _optional_clock(monitor_manager_raw, auto_stop, "stop-at", "monitor.manager.stop-at"),
    )
    return MonitorConfig(
        monitor_enabled, monitor_endpoint, timezone, monitor_schedule, monitor_power,
    )


def _parse_cos(root: dict[str, Any]) -> CosConfig:
    cos_raw = _mapping(root.get("cos"), "cos", optional=True)
    cos_enabled = _boolean(cos_raw.get("enabled"), "cos.enabled", default=False)
    return CosConfig(
        cos_enabled,
        _text(cos_raw.get("region"), "cos.region") if cos_enabled else None,
        _text(cos_raw.get("bucket"), "cos.bucket") if cos_enabled else None,
        str(cos_raw.get("prefix", "/minecraft")),
        _positive_int(cos_raw.get("expire", 3600), "cos.expire"),
    )


def load_manager(path: str | Path = CONFIG_FILE) -> ManagerConfig:
    """只读取并校验 Manager 配置。"""
    return _parse_manager(_read_root(path))


def deployment_host(path: str | Path, runtime: str) -> str:
    """只读取账号引导所需的主机，不解析运行时用户或业务配置。"""
    if runtime not in ("manager", "monitor"):
        raise ConfigError(f"未知运行时：{runtime}")
    section = _mapping(_read_root(path).get(runtime), runtime)
    return _host(section.get("host"), f"{runtime}.host")


def deployment_connection(path: str | Path, runtime: str) -> tuple[str, str]:
    """读取部署前置操作所需的 SSH 地址，不解析运行时业务配置。"""
    if runtime not in ("manager", "monitor"):
        raise ConfigError(f"未知运行时：{runtime}")
    section = _mapping(_read_root(path).get(runtime), runtime)
    return (
        _host(section.get("host"), f"{runtime}.host"),
        _user(section.get("user"), runtime),
    )


def deployment_endpoint(path: str | Path, runtime: str) -> Endpoint:
    """读取源码部署所需的 SSH 地址和目录，不解析运行时业务配置。"""
    if runtime not in ("manager", "monitor"):
        raise ConfigError(f"未知运行时：{runtime}")
    return _endpoint(
        _mapping(_read_root(path).get(runtime), runtime), runtime,
    )


def load_monitor(path: str | Path = CONFIG_FILE) -> MonitorConfig:
    """只读取并校验 Monitor 配置。"""
    return _parse_monitor(_read_root(path))


def load(path: str | Path = CONFIG_FILE) -> AppConfig:
    """读取并校验完整 Terminal 配置。"""
    root = _read_root(path)
    return AppConfig(_parse_manager(root), _parse_monitor(root), _parse_cos(root))
