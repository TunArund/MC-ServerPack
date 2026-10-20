"""校验整合包配置并渲染 Docker Compose 项目。"""

from __future__ import annotations

import hashlib
import ipaddress
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    import yaml
except ModuleNotFoundError:
    yaml = None

MANAGER_ROOT = Path(
    os.environ.get("MCSP_MANAGER_ROOT", Path(__file__).resolve().parents[1])
).resolve()
PACKS_DIR = MANAGER_ROOT / "modpacks"
MANAGER_CONFIG = MANAGER_ROOT / "data/config.yml"
PACK_ID_RE = re.compile(r"[A-Za-z0-9-]+")
MEMORY_RE = re.compile(r"([1-9]\d*)([kKmMgGtT])(?:[bB])?")
INTERVAL_RE = re.compile(r"([1-9]\d*)([mhd])")
TIME_RE = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")
PROVIDER_ID_RE = re.compile(r"[A-Za-z0-9_]+")
DEFAULT_COMMAND = ("mc-auto",)
EMPTY_JSON_DEFAULTS = {
    "banned-ips.json": "[]\n",
    "banned-players.json": "[]\n",
    "ops.json": "[]\n",
    "usercache.json": "[]\n",
    "usernamecache.json": "{}\n",
    "whitelist.json": "[]\n",
}


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class ApiSettings:
    listen: str
    port: int
    certificate: Path
    private_key: Path


def load_api_settings(path: Path = MANAGER_CONFIG) -> ApiSettings:
    if yaml is None:
        raise ConfigError("缺少 PyYAML")
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"缺少 {path}；请从 config.yml.example 复制") from exc
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"Manager 配置读取失败：{exc}") from exc
    if not isinstance(document, dict):
        raise ConfigError("Manager config.yml 必须是对象")
    api = document.get("api", {})
    if not isinstance(api, dict):
        raise ConfigError("api 必须是对象")
    listen = api.get("listen", "0.0.0.0")
    if not isinstance(listen, str):
        raise ConfigError("api.listen 必须是 IPv4 地址")
    try:
        if ipaddress.ip_address(listen).version != 4:
            raise ValueError
    except ValueError as exc:
        raise ConfigError("api.listen 必须是 IPv4 地址") from exc
    port = api.get("port", 27016)
    if type(port) is not int or not 1 <= port <= 65535:
        raise ConfigError("api.port 必须是 1 到 65535 的整数")
    root = path.parent.parent
    certificate = root / "data/tls/server.crt"
    private_key = root / "data/tls/server.key"
    if not certificate.is_file() or not private_key.is_file():
        raise ConfigError(
            "缺少 Manager TLS 证书；请运行 ./manager.sh install，"
            "或通过 Terminal 执行 ./mcsp.sh deploy manager"
        )
    return ApiSettings(listen, port, certificate, private_key)


@dataclass(frozen=True)
class PersistentPath:
    path: str
    directory: bool


@dataclass(frozen=True)
class PackSpec:
    pack_id: str
    directory: Path
    name: str
    version: str
    java: int
    memory: str
    memory_bytes: int
    port: int
    eula: bool
    jvm_args: tuple[str, ...]
    command: tuple[str, ...]
    read_only: bool
    persistent: tuple[PersistentPath, ...]
    backup_enabled: bool
    backup_interval: str
    backup_interval_seconds: int
    backup_retain: int
    backup_quiesce_seconds: int


@dataclass(frozen=True)
class ManagerLimits:
    max_running: int
    memory: str
    memory_bytes: int
    start_timeout: int
    stop_timeout: int
    schedule_enabled: bool = False
    stop_at: str = "23:00"
    notify_elast: int = 60
    notify_step: int = 2
    poweroff_enabled: bool = False
    poweroff_deadline: int = 5
    power_region: str | None = None
    power_instance_id: str | None = None
    poweroff_at: str = "23:05"
    power_provider: str = "tencent"
    timezone: str = "Asia/Shanghai"


def validate_pack_id(pack_id: str) -> str:
    if not isinstance(pack_id, str) or not PACK_ID_RE.fullmatch(pack_id) or pack_id == "template":
        raise ConfigError(f"无效整合包 ID：{pack_id}")
    return pack_id


def parse_memory(value: object) -> tuple[str, int]:
    if not isinstance(value, str):
        raise ConfigError("mem-limit 必须是类似 4g 的内存大小")
    match = MEMORY_RE.fullmatch(value.strip())
    if not match:
        raise ConfigError("mem-limit 必须是类似 4g 的内存大小")
    amount, unit = int(match.group(1)), match.group(2).lower()
    multiplier = {"k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}[unit]
    return f"{amount}{unit}", amount * multiplier


def parse_interval(value: object) -> tuple[str, int]:
    if not isinstance(value, str):
        raise ConfigError("backup.interval 必须是类似 30m、6h 或 1d 的时长")
    normalized = value.strip().lower()
    match = INTERVAL_RE.fullmatch(normalized)
    if not match:
        raise ConfigError("backup.interval 必须是类似 30m、6h 或 1d 的时长")
    amount, unit = int(match.group(1)), match.group(2)
    multiplier = {"m": 60, "h": 3600, "d": 86400}[unit]
    return normalized, amount * multiplier


def normalize_persistent(value: object) -> PersistentPath:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("read-only.persistent 中的路径必须是非空字符串")
    raw = value.strip().replace("\\", "/")
    directory = raw.endswith("/")
    raw = raw.rstrip("/")
    path = PurePosixPath(raw)
    if path.is_absolute() or not raw or any(part in ("", ".", "..") for part in path.parts):
        raise ConfigError(f"持久化路径必须是安全的相对路径：{value}")
    return PersistentPath(path.as_posix(), directory)


def _positive_integer(value: object, field: str, *, minimum: int = 1) -> int:
    if type(value) is not int or value < minimum:
        raise ConfigError(f"{field} 必须是不小于 {minimum} 的整数")
    return value


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


def _timezone(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{field} 必须是非空字符串")
    name = value.strip()
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"{field} 必须是有效的 IANA 时区，例如 Asia/Shanghai") from exc
    return name


def load_limits(path: Path = MANAGER_CONFIG) -> ManagerLimits:
    if yaml is None:
        raise ConfigError("缺少 PyYAML")
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"缺少 {path}；请从 config.yml.example 复制") from exc
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"Manager 配置读取失败：{exc}") from exc
    if not isinstance(document, dict):
        raise ConfigError("manager/config.yml 必须是对象")
    timezone = _timezone(document.get("timezone"), "timezone")
    modpack = document.get("modpack")
    if not isinstance(modpack, dict):
        legacy = any(
            key in document
            for key in ("max-running", "mem-limit", "start-timeout", "stop-timeout")
        )
        if legacy:
            raise ConfigError(
                f"{path} 使用了旧版平铺配置；请将 max-running、mem-limit、"
                "start-timeout 和 stop-timeout 移入 modpack: 对象"
            )
        raise ConfigError(f"{path} 的 modpack 必须是对象")
    memory, memory_bytes = parse_memory(modpack.get("mem-limit"))
    schedule_enabled = _boolean(modpack.get("auto-stop"), "modpack.auto-stop")
    poweroff_enabled = _boolean(document.get("auto-stop"), "auto-stop")
    power = document.get("power-api")
    region = None
    instance_id = None
    provider = "tencent"
    if poweroff_enabled:
        if not schedule_enabled:
            raise ConfigError("auto-stop 需要 modpack.auto-stop=true")
        if not isinstance(power, dict):
            raise ConfigError("云关机需要 power-api")
        region = power.get("instance-region")
        instance_id = power.get("instance-id")
        provider = power.get("provider")
        if not isinstance(region, str) or not region.strip():
            raise ConfigError("power-api.instance-region 必须是非空字符串")
        if not isinstance(instance_id, str) or not instance_id.strip():
            raise ConfigError("power-api.instance-id 必须是非空字符串")
        if not isinstance(provider, str) or not provider.strip():
            raise ConfigError("power-api.provider 必须是非空 provider ID")
        provider = provider.strip()
        if not PROVIDER_ID_RE.fullmatch(provider):
            raise ConfigError("power-api.provider 仅允许大小写字母、数字和下划线")
    return ManagerLimits(
        _positive_integer(modpack.get("max-running", 1), "modpack.max-running"),
        memory,
        memory_bytes,
        _positive_integer(modpack.get("start-timeout", 300), "modpack.start-timeout"),
        _positive_integer(modpack.get("stop-timeout", 180), "modpack.stop-timeout"),
        schedule_enabled,
        _clock(modpack.get("stop-at", "23:00"), "modpack.stop-at"),
        _positive_integer(modpack.get("stop-notify-elast", 60), "modpack.stop-notify-elast"),
        _positive_integer(modpack.get("stop-notify-step", 2), "modpack.stop-notify-step", minimum=2),
        poweroff_enabled,
        5,
        region.strip() if isinstance(region, str) else None,
        instance_id.strip() if isinstance(instance_id, str) else None,
        _clock(document.get("stop-at", "23:05"), "stop-at"),
        provider,
        timezone,
    )


def runtime_image(java: int, manager_root: Path = MANAGER_ROOT) -> str:
    digest = hashlib.sha256()
    paths = (
        manager_root / "runtime" / f"java{java}.dockerfile",
        manager_root / "runtime" / "entrypoint.sh",
    )
    for path in paths:
        try:
            digest.update(path.read_bytes())
        except OSError as exc:
            raise ConfigError(f"无法读取运行时文件 {path}：{exc}") from exc
    return f"mc-java:{java}-{digest.hexdigest()[:12]}"


def load_pack(pack_id: str, packs_dir: Path = PACKS_DIR) -> PackSpec:
    validate_pack_id(pack_id)
    if yaml is None:
        raise ConfigError("缺少 PyYAML")
    directory = packs_dir / pack_id
    config_file = directory / "config.yml"
    if not (directory / "src").is_dir():
        raise ConfigError(f"{pack_id} 缺少 src/ 目录")
    try:
        document = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"{pack_id} 缺少 config.yml") from exc
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"{pack_id} 配置读取失败：{exc}") from exc
    if not isinstance(document, dict):
        raise ConfigError(f"{pack_id}/config.yml 必须是对象")

    name = document.get("name", pack_id)
    version = document.get("version", "unknown")
    java = document.get("java")
    port = document.get("port", 25565)
    eula = document.get("eula", False)
    jvm_args = document.get("jvm-args", [])
    command = document.get("command", list(DEFAULT_COMMAND))
    read_only = document.get("read-only", {})
    backup = document.get("backup", {})
    if not isinstance(name, str) or not name.strip():
        raise ConfigError("name 必须是非空字符串")
    if not isinstance(version, (str, int, float)) or isinstance(version, bool):
        raise ConfigError("version 必须是字符串或数字")
    if type(java) is not int or java not in (8, 17, 21):
        raise ConfigError("java 只能是 8、17 或 21")
    memory, memory_bytes = parse_memory(document.get("mem-limit"))
    if type(port) is not int or not 1 <= port <= 65535:
        raise ConfigError("port 必须是 1 到 65535 的整数")
    if type(eula) is not bool:
        raise ConfigError("eula 必须是 true 或 false")
    if not isinstance(jvm_args, list) or not all(
        isinstance(item, str) and item.strip() for item in jvm_args
    ):
        raise ConfigError("jvm-args 必须是非空字符串数组")
    if not isinstance(command, list) or not command or not all(
        isinstance(item, str) and item.strip() for item in command
    ):
        raise ConfigError("command 必须是非空字符串数组")
    if not isinstance(read_only, dict):
        raise ConfigError("read-only 必须是包含 enabled/persistent 的对象")
    enabled = read_only.get("enabled", False)
    raw_persistent = read_only.get("persistent", [])
    if type(enabled) is not bool:
        raise ConfigError("read-only.enabled 必须是 true 或 false")
    if not isinstance(raw_persistent, list):
        raise ConfigError("read-only.persistent 必须是数组")
    if not isinstance(backup, dict):
        raise ConfigError("backup 必须是对象")
    backup_enabled = backup.get("enabled", False)
    if type(backup_enabled) is not bool:
        raise ConfigError("backup.enabled 必须是 true 或 false")
    backup_interval, backup_interval_seconds = parse_interval(backup.get("interval", "6h"))
    backup_retain = _positive_integer(backup.get("retain", 7), "backup.retain")
    quiesce = _positive_integer(backup.get("quiesce-seconds", 5), "backup.quiesce-seconds", minimum=0)
    persistent = tuple(normalize_persistent(item) for item in raw_persistent)
    seen: set[str] = set()
    for item in persistent:
        if item.path in seen:
            raise ConfigError(f"重复的持久化路径：{item.path}")
        seen.add(item.path)
    for parent in persistent:
        parent_path = PurePosixPath(parent.path)
        if parent.directory and any(
            child.path != parent.path and parent_path in PurePosixPath(child.path).parents
            for child in persistent
        ):
            raise ConfigError(f"持久化目录 {parent.path}/ 已包含子路径")
    return PackSpec(
        pack_id, directory, name.strip(), str(version), java, memory, memory_bytes,
        port, eula, tuple(jvm_args), tuple(command), enabled, persistent, backup_enabled,
        backup_interval, backup_interval_seconds, backup_retain, quiesce,
    )


def prepare_persistent(spec: PackSpec) -> None:
    source = spec.directory / "src"
    data = spec.directory / "data"
    data.mkdir(exist_ok=True)
    for item in spec.persistent:
        source_path = source / item.path
        data_path = data / item.path
        data_path.parent.mkdir(parents=True, exist_ok=True)
        if item.directory:
            if source_path.exists() and not source_path.is_dir():
                raise ConfigError(f"持久化路径类型冲突：{item.path}/")
            source_path.mkdir(parents=True, exist_ok=True)
            if not data_path.exists():
                shutil.copytree(source_path, data_path)
            elif not data_path.is_dir():
                raise ConfigError(f"持久化路径类型冲突：{item.path}/")
        else:
            if not source_path.is_file() and not data_path.is_file():
                raise ConfigError(f"持久化文件不存在：{item.path}")
            if (
                source_path.is_symlink() or data_path.is_symlink()
                or (source_path.exists() and not source_path.is_file())
                or (data_path.exists() and not data_path.is_file())
            ):
                raise ConfigError(f"持久化路径类型冲突：{item.path}")
            # 先在外层 src bind 中创建目标，避免 Docker 为嵌套文件挂载
            # 生成 root:root 的空占位文件，导致后续 Manager 无法更新配置。
            if not source_path.exists():
                source_path.touch()
            if not data_path.exists():
                shutil.copy2(source_path, data_path)
    for name, default in EMPTY_JSON_DEFAULTS.items():
        source_path = source / name
        data_path = data / name
        for path in (source_path, data_path):
            if path.is_file() and not path.is_symlink() and path.stat().st_size == 0:
                path.write_text(default, encoding="utf-8")


def render_compose(spec: PackSpec) -> Path:
    if yaml is None:
        raise ConfigError("缺少 PyYAML")
    prepare_persistent(spec)
    volumes: list[dict[str, object]] = [{
        "type": "bind", "source": "./src", "target": "/server",
        "read_only": spec.read_only, "bind": {"create_host_path": False},
    }]
    for item in spec.persistent:
        volumes.append({
            "type": "bind", "source": f"./data/{item.path}",
            "target": f"/server/{item.path}", "bind": {"create_host_path": False},
        })
    manager_root = spec.directory.parents[1]
    command = list(spec.command)
    if command[0] == "java":
        command[1:1] = spec.jvm_args
    service = {
        # data/state.json 是自启真源；Docker daemon 不得自行复活 disabled 整合包。
        "image": runtime_image(spec.java, manager_root), "init": True, "restart": "no",
        "working_dir": "/server", "stop_grace_period": "${MC_DOCKER_GRACE:-190}s",
        "command": command,
        "user": "${MC_CONTAINER_UID:-1000}:${MC_CONTAINER_GID:-1000}",
        "mem_limit": spec.memory, "ports": [f"{spec.port}:{spec.port}"],
        "environment": {"HOME": "/tmp", "MC_STOP_TIMEOUT": "${MC_STOP_TIMEOUT:-180}",
                        "MC_SERVER_PORT": str(spec.port), "TZ": "${TZ:-UTC}"},
        "healthcheck": {
            "test": ["CMD", "bash", "-c", "exec 3<>/dev/tcp/127.0.0.1/$${MC_SERVER_PORT:-25565}"],
            "interval": "10s", "timeout": "3s",
            "start_period": "${MC_START_TIMEOUT:-300}s", "retries": 3,
        },
        "labels": {"mc-manager.managed": "true", "mc-manager.pack": spec.pack_id},
        "volumes": volumes,
    }
    document = {"name": f"mc-{spec.pack_id}",
                "x-modpack": {"name": spec.name, "version": spec.version, "java": spec.java},
                "services": {"minecraft": service}}
    target = spec.directory / "compose.yml"
    content = "# 由 manager 根据 config.yml 生成，请勿手工编辑。\n" + yaml.safe_dump(
        document, sort_keys=False, allow_unicode=True,
    )
    if not target.exists() or target.read_text(encoding="utf-8") != content:
        target.write_text(content, encoding="utf-8")
    return target
