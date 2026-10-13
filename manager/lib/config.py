#!/usr/bin/env python3
"""Parse manager configuration and render per-pack Compose files."""

from __future__ import annotations

import argparse
import re
import shutil
import os
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

try:
    import yaml
except ModuleNotFoundError:
    yaml = None

ROOT = Path(__file__).resolve().parent.parent
PACKS = ROOT / "modpacks"
CONFIG = Path(os.environ.get("MC_MANAGER_CONFIG", ROOT / "config.yml"))
if not CONFIG.is_file() and (ROOT.parent / "config.yml").is_file():
    CONFIG = ROOT.parent / "config.yml"
PACK_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
SIZE_RE = re.compile(r"^([0-9]+(?:\.[0-9]+)?)([KMGT]?)B?$", re.I)
DURATION_RE = re.compile(r"^([0-9]+)([smhd])$", re.I)
TIME_RE = re.compile(r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")
DEFAULT_WRITABLE = (
    "world/",
    "logs/",
    "crash-reports/",
    "usercache.json",
    "usernamecache.json",
    "banned-ips.json",
    "banned-players.json",
    "ops.json",
    "whitelist.json",
    "eula.txt",
    "server.properties",
)

INITIAL_FILE_CONTENT = {
    "usercache.json": "[]\n",
    "usernamecache.json": "{}\n",
    "banned-ips.json": "[]\n",
    "banned-players.json": "[]\n",
    "ops.json": "[]\n",
    "whitelist.json": "[]\n",
    "eula.txt": "# Read https://aka.ms/MinecraftEULA before changing this value.\neula=false\n",
    "server.properties": "",
}


class ConfigError(RuntimeError):
    pass


def parse_size(value: object) -> int:
    if not isinstance(value, str) or not (match := SIZE_RE.fullmatch(value.strip())):
        raise ConfigError(f"无效的内存值：{value!r}")
    exponent = {"": 0, "K": 1, "M": 2, "G": 3, "T": 4}[match.group(2).upper()]
    return int(float(match.group(1)) * 1024**exponent)


def parse_duration(value: object) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        if value > 0:
            return value
    elif isinstance(value, str) and (match := DURATION_RE.fullmatch(value.strip())):
        return int(match.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[match.group(2).lower()]
    raise ConfigError(f"无效的时长：{value!r}（例如 30s、5m、1h）")


@dataclass(frozen=True)
class GlobalConfig:
    max_running: int
    overflow_policy: str
    max_memory: str
    max_memory_bytes: int
    start_timeout_seconds: int
    stop_timeout_seconds: int

    @classmethod
    def load(cls) -> "GlobalConfig":
        if yaml is None:
            raise ConfigError("缺少 PyYAML；请安装 lib/requirements.txt")
        try:
            doc = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ConfigError(f"缺少 {CONFIG}") from exc
        except (OSError, yaml.YAMLError) as exc:
            raise ConfigError(f"config.yml 读取失败：{exc}") from exc
        if not isinstance(doc, dict) or not isinstance(doc.get("manager"), dict):
            raise ConfigError("config.yml 缺少 manager 配置")
        modpack = doc["manager"].get("modpack", {})
        if not isinstance(modpack, dict):
            raise ConfigError("manager.modpack 必须是对象")
        maximum = modpack.get("max-running", 1)
        policy = modpack.get("overnum-action", "reject")
        memory = modpack.get("mem-limit", "0")
        if type(maximum) is not int or maximum < 1:
            raise ConfigError("modpack.max-running 必须是正整数")
        if policy not in ("reject", "rotate"):
            raise ConfigError("manager.modpack.overnum-action 只能是 reject 或 rotate")
        return cls(
            maximum, policy, memory, parse_size(memory),
            parse_duration(modpack.get("start-timeout", "5m")),
            parse_duration(modpack.get("stop-timeout", "3m")),
        )

    def get(self, key: str) -> str:
        values = {
            "max-running": self.max_running,
            "overflow-policy": self.overflow_policy,
            "max-memory": self.max_memory,
            "max-memory-bytes": self.max_memory_bytes,
            "start-timeout-seconds": self.start_timeout_seconds,
            "stop-timeout-seconds": self.stop_timeout_seconds,
        }
        if key not in values:
            raise ConfigError(f"未知全局配置项：{key}")
        return str(values[key])


@dataclass(frozen=True)
class WritablePath:
    path: str
    directory: bool


@dataclass(frozen=True)
class PackSpec:
    pack: str
    name: str
    version: str
    java: int
    memory: str
    memory_bytes: int
    port: int
    command: tuple[str, ...]
    writable: tuple[WritablePath, ...]
    backup_enabled: bool
    backup_interval: str
    backup_retain: int
    backup_cron: str

    @property
    def directory(self) -> Path:
        return PACKS / self.pack

    def get(self, key: str) -> str:
        values = {
            "name": self.name, "version": self.version, "java": self.java,
            "memory": self.memory, "memory-bytes": self.memory_bytes, "port": self.port,
            "command": "\0".join(self.command),
            "backup-enabled": str(self.backup_enabled).lower(),
            "backup-interval": self.backup_interval,
            "backup-retain": self.backup_retain,
            "backup-cron-expression": self.backup_cron,
        }
        if key not in values:
            raise ConfigError(f"未知整合包配置项：{key}")
        return str(values[key])


def validate_pack_id(pack: str) -> None:
    if not PACK_RE.fullmatch(pack):
        raise ConfigError(f"无效的整合包 ID：{pack}")


def normalize_writable(raw: object) -> WritablePath:
    if not isinstance(raw, str) or not raw:
        raise ConfigError("dynamic-paths 的每一项都必须是非空字符串")
    if "\\" in raw or raw.startswith("/"):
        raise ConfigError(f"动态路径必须是 POSIX 相对路径：{raw}")
    directory = raw.endswith("/")
    normalized = raw.rstrip("/")
    path = PurePosixPath(normalized)
    if not normalized or any(part in ("", ".", "..") for part in path.parts):
        raise ConfigError(f"不安全的动态路径：{raw}")
    return WritablePath(path.as_posix(), directory)


def backup_cron_expression(interval: str) -> str:
    match = DURATION_RE.fullmatch(interval.strip())
    if not match:
        raise ConfigError("backup.interval 应类似 30m、6h 或 1d")
    amount, unit = int(match.group(1)), match.group(2).lower()
    if unit == "m" and 1 <= amount <= 59 and 60 % amount == 0:
        return f"*/{amount} * * * *"
    if unit == "h" and 1 <= amount <= 23 and 24 % amount == 0:
        return f"0 */{amount} * * *"
    if unit == "d" and amount == 1:
        return "0 4 * * *"
    raise ConfigError(
        "backup.interval 当前支持可整除一小时的分钟、可整除一天的小时，或 1d"
    )


def load_pack(pack: str) -> PackSpec:
    validate_pack_id(pack)
    if yaml is None:
        raise ConfigError("缺少 PyYAML；请安装 lib/requirements.txt")
    directory = PACKS / pack
    spec_file = directory / "config.yml"
    if not (directory / "src").is_dir() or not (directory / "data").is_dir():
        raise ConfigError(f"modpacks/{pack} 必须包含 config.yml、src/ 和 data/")
    try:
        doc = yaml.safe_load(spec_file.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"缺少 {spec_file}") from exc
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"config.yml 读取失败：{exc}") from exc
    if not isinstance(doc, dict):
        raise ConfigError(f"modpacks/{pack}/config.yml 必须是对象")
    name, version = doc.get("name", pack), doc.get("version", "unknown")
    java, memory, port = doc.get("java"), doc.get("memory"), doc.get("port", 25565)
    command = doc.get("command", ["bash", "./run.sh", "nogui"])
    if "writable-paths" in doc:
        raise ConfigError("writable-paths 已更名为 dynamic-paths")
    raw_writable = doc.get("dynamic-paths", list(DEFAULT_WRITABLE))
    backup = doc.get("backup", {})
    if not isinstance(name, str) or not name:
        raise ConfigError("config.yml 的 name 必须是非空字符串")
    if not isinstance(version, (str, int, float)) or isinstance(version, bool):
        raise ConfigError("config.yml 的 version 必须是字符串或数字")
    if type(java) is not int or java < 1:
        raise ConfigError("config.yml 的 java 必须是正整数")
    memory_bytes = parse_size(memory)
    if type(port) is not int or not 1 <= port <= 65535:
        raise ConfigError("config.yml 的 port 必须是 1 到 65535 的整数")
    if not isinstance(command, list) or not command or not all(isinstance(x, str) and x for x in command):
        raise ConfigError("config.yml 的 command 必须是非空字符串数组")
    if not isinstance(raw_writable, list):
        raise ConfigError("config.yml 的 dynamic-paths 必须是数组")
    if not isinstance(backup, dict):
        raise ConfigError("config.yml 的 backup 必须是对象")
    backup_enabled = backup.get("enabled", False)
    backup_interval = backup.get("interval", "6h")
    backup_retain = backup.get("retain", 7)
    if type(backup_enabled) is not bool:
        raise ConfigError("backup.enabled 必须是 true 或 false")
    if not isinstance(backup_interval, str):
        raise ConfigError("backup.interval 必须是字符串")
    if type(backup_retain) is not int or backup_retain < 1:
        raise ConfigError("backup.retain 必须是正整数")
    backup_cron = backup_cron_expression(backup_interval)
    writable = tuple(normalize_writable(item) for item in raw_writable)
    seen: set[str] = set()
    for item in writable:
        if item.path in seen:
            raise ConfigError(f"重复的动态路径：{item.path}")
        seen.add(item.path)
    for parent in writable:
        if parent.directory and any(
            child.path != parent.path and PurePosixPath(parent.path) in PurePosixPath(child.path).parents
            for child in writable
        ):
            raise ConfigError(f"动态目录 {parent.path}/ 已包含其子路径，无需重复声明")
    return PackSpec(
        pack, name, str(version), java, memory, memory_bytes, port, tuple(command),
        writable, backup_enabled, backup_interval, backup_retain, backup_cron,
    )


def prepare_mounts(spec: PackSpec) -> None:
    source, data = spec.directory / "src", spec.directory / "data"
    for item in spec.writable:
        src_path, data_path = source / item.path, data / item.path
        src_path.parent.mkdir(parents=True, exist_ok=True)
        data_path.parent.mkdir(parents=True, exist_ok=True)
        if item.directory:
            if (src_path.exists() and not src_path.is_dir()) or (data_path.exists() and not data_path.is_dir()):
                raise ConfigError(f"动态目录类型冲突：{item.path}/")
            src_path.mkdir(exist_ok=True)
            if not data_path.exists():
                shutil.copytree(src_path, data_path)
            elif not any(data_path.iterdir()) and any(src_path.iterdir()):
                # 仅初始化空 data；已有持久化内容永不覆盖。
                shutil.copytree(src_path, data_path, dirs_exist_ok=True)
        else:
            if src_path.is_dir() or data_path.is_dir():
                raise ConfigError(f"动态文件类型冲突：{item.path}")
            if not data_path.exists():
                if src_path.is_file():
                    shutil.copy2(src_path, data_path)
                elif item.path in INITIAL_FILE_CONTENT:
                    data_path.write_text(INITIAL_FILE_CONTENT[item.path], encoding="utf-8")
                else:
                    raise ConfigError(
                        f"动态文件 {item.path} 不存在；请先在 src/ 或 data/ 创建它"
                    )
            elif (
                data_path.stat().st_size == 0
                and INITIAL_FILE_CONTENT.get(item.path)
                and (not src_path.is_file() or src_path.stat().st_size == 0)
            ):
                # Repair placeholders made by older manager versions. An empty
                # JSON file is not a valid Minecraft cache/list file.
                data_path.write_text(INITIAL_FILE_CONTENT[item.path], encoding="utf-8")
            if not src_path.exists():
                src_path.touch()


def render(spec: PackSpec) -> Path:
    global_config = GlobalConfig.load()
    if global_config.max_memory_bytes > 0 and spec.memory_bytes > global_config.max_memory_bytes:
        raise ConfigError(f"整合包 {spec.pack} 的内存 {spec.memory} 超过全局上限 {global_config.max_memory}")
    prepare_mounts(spec)
    volumes: list[dict[str, object]] = [{
        "type": "bind", "source": "./src", "target": "/server", "read_only": True,
        "bind": {"create_host_path": False},
    }]
    for item in spec.writable:
        volumes.append({
            "type": "bind", "source": f"./data/{item.path}", "target": f"/server/{item.path}",
            "bind": {"create_host_path": False},
        })
    document = {
        "name": f"mc-{spec.pack}",
        "x-modpack": {"name": spec.name, "version": spec.version, "java": spec.java,
                      "dynamic-paths": [item.path + ("/" if item.directory else "") for item in spec.writable]},
        "services": {"minecraft": {
            "image": f"mc-java:{spec.java}", "command": list(spec.command),
            "user": "${MC_CONTAINER_UID:?}:${MC_CONTAINER_GID:?}",
            "mem_limit": spec.memory, "ports": [f"{spec.port}:{spec.port}"],
            "environment": {"MC_SERVER_PORT": str(spec.port)}, "volumes": volumes,
        }},
    }
    target = spec.directory / "compose.yml"
    content = "# 此文件由 config.yml 自动生成，请勿直接编辑。\n" + yaml.safe_dump(
        document, sort_keys=False, allow_unicode=True
    )
    if not target.exists() or target.read_text(encoding="utf-8") != content:
        target.write_text(content, encoding="utf-8")
    return target


def list_packs() -> list[str]:
    return sorted(path.parent.name for path in PACKS.glob("*/config.yml") if path.is_file())


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)
    glob = sub.add_parser("global"); glob.add_argument("key")
    pack = sub.add_parser("pack"); pack.add_argument("pack"); pack.add_argument("key")
    val = sub.add_parser("validate"); val.add_argument("pack")
    ren = sub.add_parser("render"); ren.add_argument("pack")
    dur = sub.add_parser("duration"); dur.add_argument("value")
    sub.add_parser("packs")
    args = parser.parse_args()
    if args.action == "global": print(GlobalConfig.load().get(args.key))
    elif args.action == "pack": print(load_pack(args.pack).get(args.key))
    elif args.action == "validate": load_pack(args.pack); GlobalConfig.load()
    elif args.action == "render": print(render(load_pack(args.pack)))
    elif args.action == "duration": print(parse_duration(args.value))
    elif args.action == "packs": print("\n".join(list_packs()))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ConfigError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(2)
