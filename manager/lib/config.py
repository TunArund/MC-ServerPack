"""Validate modpack configuration and render its Docker Compose project."""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

try:
    import yaml
except ModuleNotFoundError:
    yaml = None

MANAGER_ROOT = Path(__file__).resolve().parents[1]
PACKS_DIR = MANAGER_ROOT / "modpacks"
PACK_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
MEMORY_RE = re.compile(r"([1-9]\d*)([kKmMgGtT])(?:[bB])?")
DEFAULT_COMMAND = ("bash", "./run.sh", "nogui")


class ConfigError(RuntimeError):
    pass


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
    command: tuple[str, ...]
    read_only: bool
    persistent: tuple[PersistentPath, ...]


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
    command = document.get("command", list(DEFAULT_COMMAND))
    read_only = document.get("read-only", {})
    if not isinstance(name, str) or not name.strip():
        raise ConfigError("name 必须是非空字符串")
    if not isinstance(version, (str, int, float)) or isinstance(version, bool):
        raise ConfigError("version 必须是字符串或数字")
    if type(java) is not int or java not in (8, 17, 21):
        raise ConfigError("java 只能是 8、17 或 21")
    memory, memory_bytes = parse_memory(document.get("mem-limit"))
    if type(port) is not int or not 1 <= port <= 65535:
        raise ConfigError("port 必须是 1 到 65535 的整数")
    if not isinstance(command, list) or not command or not all(isinstance(item, str) and item for item in command):
        raise ConfigError("command 必须是非空字符串数组")
    if not isinstance(read_only, dict):
        raise ConfigError("read-only 必须是包含 enabled/persistent 的对象")
    enabled = read_only.get("enabled", False)
    raw_persistent = read_only.get("persistent", [])
    if type(enabled) is not bool:
        raise ConfigError("read-only.enabled 必须是 true 或 false")
    if not isinstance(raw_persistent, list):
        raise ConfigError("read-only.persistent 必须是数组")
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
        port, tuple(command), enabled, persistent,
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
            if source_path.is_dir() or data_path.is_dir():
                raise ConfigError(f"持久化路径类型冲突：{item.path}")
            if not data_path.exists():
                shutil.copy2(source_path, data_path)


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
    service = {
        "image": f"mc-java:{spec.java}", "init": True, "restart": "unless-stopped",
        "working_dir": "/server", "stop_grace_period": "${MC_DOCKER_GRACE:-190}s",
        "command": list(spec.command), "user": "${MC_CONTAINER_UID:?}:${MC_CONTAINER_GID:?}",
        "mem_limit": spec.memory, "ports": [f"{spec.port}:{spec.port}"],
        "environment": {"HOME": "/tmp", "MC_STOP_TIMEOUT": "${MC_STOP_TIMEOUT:-180}",
                        "MC_SERVER_PORT": str(spec.port)},
        "healthcheck": {
            "test": ["CMD", "bash", "-c", "exec 3<>/dev/tcp/127.0.0.1/$${MC_SERVER_PORT:-25565}"],
            "interval": "10s", "timeout": "3s", "start_period": "120s", "retries": 3,
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
