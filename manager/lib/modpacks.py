"""Manager 整合包生命周期应用服务。"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlopen

from .backup import create_backup, resolve_backup, restore_backup
from .config import (
    MANAGER_ROOT,
    PACKS_DIR,
    ManagerLimits,
    PackSpec,
    load_limits,
    load_pack,
    render_compose,
    validate_pack_id,
)
from .docker import DockerClient


class ModpackError(RuntimeError):
    pass


VANILLA_MANIFEST = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
MOJANG_HOSTS = {
    "piston-meta.mojang.com", "piston-data.mojang.com", "launcher.mojang.com",
}
VERSION_RE = re.compile(r"[A-Za-z0-9._-]+")


def _official_url(url: object, field: str) -> str:
    if not isinstance(url, str):
        raise ModpackError(f"Mojang 元数据缺少 {field}")
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in MOJANG_HOSTS:
        raise ModpackError(f"Mojang 元数据返回了非官方 {field} URL")
    return url


def _read_url(url: str, *, limit: int) -> bytes:
    try:
        with urlopen(url, timeout=60) as response:
            content = response.read(limit + 1)
    except OSError as exc:
        raise ModpackError(f"下载失败：{url}：{exc}") from exc
    if len(content) > limit:
        raise ModpackError(f"下载内容超过限制：{url}")
    return content


def _read_json(url: str) -> dict:
    try:
        document = json.loads(_read_url(url, limit=8 * 1024 * 1024))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ModpackError(f"Mojang 元数据无效：{url}") from exc
    if not isinstance(document, dict):
        raise ModpackError(f"Mojang 元数据不是对象：{url}")
    return document


def _java_version(metadata: dict, version: str) -> int:
    java = metadata.get("javaVersion")
    major = java.get("majorVersion") if isinstance(java, dict) else None
    if isinstance(major, int):
        for supported in (8, 17, 21):
            if major <= supported:
                return supported
        raise ModpackError(f"Minecraft {version} 需要 Java {major}，当前没有对应镜像")
    match = re.fullmatch(r"1\.(\d+)(?:\.(\d+))?", version)
    if not match:
        raise ModpackError(f"Minecraft {version} 缺少 Java 版本元数据")
    minor, patch = int(match.group(1)), int(match.group(2) or 0)
    if (minor, patch) >= (20, 5):
        return 21
    if minor >= 17:
        return 17
    return 8


def install_vanilla(directory: Path, requested: str, *, accept_eula: bool) -> str:
    if requested != "latest" and not VERSION_RE.fullmatch(requested):
        raise ModpackError(f"无效 Minecraft 版本：{requested}")
    manifest = _read_json(VANILLA_MANIFEST)
    if requested == "latest":
        latest = manifest.get("latest")
        requested = latest.get("release") if isinstance(latest, dict) else None
    versions = manifest.get("versions")
    if not isinstance(requested, str) or not isinstance(versions, list):
        raise ModpackError("Mojang 版本清单缺少正式版信息")
    matches = [item for item in versions if isinstance(item, dict) and item.get("id") == requested]
    if len(matches) != 1:
        raise ModpackError(f"Mojang 版本清单中找不到：{requested}")
    metadata_url = _official_url(matches[0].get("url"), "版本元数据")
    metadata = _read_json(metadata_url)
    downloads = metadata.get("downloads")
    server = downloads.get("server") if isinstance(downloads, dict) else None
    if not isinstance(server, dict):
        raise ModpackError(f"Minecraft {requested} 没有官方服务端下载")
    server_url = _official_url(server.get("url"), "服务端")
    expected = server.get("sha1")
    size = server.get("size")
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{40}", expected):
        raise ModpackError("Mojang 服务端元数据缺少有效 SHA-1")
    if not isinstance(size, int) or size <= 0 or size > 256 * 1024 * 1024:
        raise ModpackError("Mojang 服务端元数据缺少有效文件大小")
    print(f"正在下载 Minecraft {requested} 官方服务端", flush=True)
    content = _read_url(server_url, limit=size)
    if len(content) != size or hashlib.sha1(content).hexdigest() != expected:
        raise ModpackError("Minecraft 服务端文件大小或 SHA-1 校验失败")

    source = directory / "src"
    temporary = source / ".server.jar.part"
    temporary.write_bytes(content)
    temporary.replace(source / "server.jar")
    (source / "eula.txt").write_text(
        f"eula={'true' if accept_eula else 'false'}\n", encoding="utf-8",
    )
    config_file = directory / "config.yml"
    config = config_file.read_text(encoding="utf-8")
    config = re.sub(
        r"(?m)^version:\s*.*$", f"version: '{requested}'", config, count=1,
    )
    config = re.sub(
        r"(?m)^java:\s*.*$", f"java: {_java_version(metadata, requested)}", config,
        count=1,
    )
    config += "command: [java, -jar, server.jar, nogui]\n"
    config_file.write_text(config, encoding="utf-8")
    return requested


class ModpackManager:
    def __init__(
        self, manager_root: Path = MANAGER_ROOT, docker: DockerClient | None = None,
        limits: ManagerLimits | None = None,
    ) -> None:
        self.manager_root = manager_root
        self.packs_dir = manager_root / "modpacks" if manager_root != MANAGER_ROOT else PACKS_DIR
        self.docker = docker or DockerClient(manager_root)
        self._limits = limits

    @property
    def limits(self) -> ManagerLimits:
        if self._limits is None:
            self._limits = load_limits(self.manager_root / "config.yml")
        return self._limits

    def spec(self, pack_id: str) -> PackSpec:
        return load_pack(pack_id, self.packs_dir)

    def specs(self) -> list[PackSpec]:
        result = []
        for config_file in sorted(self.packs_dir.glob("*/config.yml")):
            if config_file.parent.name != "template":
                result.append(self.spec(config_file.parent.name))
        return result

    def create(
        self, pack_id: str, *, vanilla: str | None = None,
        accept_eula: bool = False, force: bool = False,
    ) -> Path:
        validate_pack_id(pack_id)
        target = self.packs_dir / pack_id
        exists = target.exists() or target.is_symlink()
        if exists and not force:
            raise ModpackError(f"整合包已存在：{pack_id}")
        template = self.packs_dir / "template"
        if not template.is_dir():
            raise ModpackError("缺少 modpacks/template")
        if accept_eula and vanilla is None:
            raise ModpackError("--accept-eula 只能与 --vanilla 一起使用")

        temporary_root = self.manager_root / "tmp"
        temporary_root.mkdir(parents=True, exist_ok=True)
        staging = temporary_root / f"{pack_id}.create-{uuid.uuid4().hex}"
        backup: Path | None = None
        try:
            shutil.copytree(
                template, staging,
                ignore=shutil.ignore_patterns("compose.yml", "__pycache__"),
            )
            config_file = staging / "config.yml"
            content = config_file.read_text(encoding="utf-8")
            content = re.sub(r"(?m)^name:\s*.*$", f"name: {pack_id}", content, count=1)
            config_file.write_text(content, encoding="utf-8")
            for name in ("src", "data", "backups"):
                (staging / name).mkdir(exist_ok=True)
            if vanilla is not None:
                install_vanilla(staging, vanilla, accept_eula=accept_eula)

            if exists:
                backup = temporary_root / f"{pack_id}.replace-{uuid.uuid4().hex}"
                target.replace(backup)
            try:
                staging.replace(target)
            except Exception:
                if backup is not None:
                    backup.replace(target)
                raise
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        if backup is not None:
            if backup.is_dir() and not backup.is_symlink():
                shutil.rmtree(backup, ignore_errors=True)
            else:
                backup.unlink(missing_ok=True)
        return target

    def configure(self, pack_id: str) -> Path:
        return render_compose(self.spec(pack_id))

    def _running(self) -> list[PackSpec]:
        return [spec for spec in self.specs() if self.docker.running(spec)]

    def broadcast_all(self, minutes: int) -> tuple[list[str], list[tuple[str, str]]]:
        sent: list[str] = []
        failed: list[tuple[str, str]] = []
        for spec in self.specs():
            try:
                if self.docker.running(spec):
                    self.docker.console(spec, f"say 服务器将在 {minutes} 分钟后关闭")
                    sent.append(spec.pack_id)
            except Exception as exc:
                failed.append((spec.pack_id, str(exc)))
        return sent, failed

    def _check_capacity(self, wanted: PackSpec, active: list[PackSpec]) -> None:
        if any(spec.port == wanted.port for spec in active):
            owner = next(spec.pack_id for spec in active if spec.port == wanted.port)
            raise ModpackError(f"端口 {wanted.port} 已被 {owner} 使用")
        if len(active) >= self.limits.max_running:
            raise ModpackError(f"已达最大运行数 {self.limits.max_running}；请先显式停止其他整合包")
        total = sum(spec.memory_bytes for spec in active) + wanted.memory_bytes
        if total > self.limits.memory_bytes:
            raise ModpackError(f"启动后内存上限将超过 {self.limits.memory}")

    def start(self, pack_id: str) -> None:
        wanted = self.spec(pack_id)
        if self.docker.running(wanted):
            return
        self._check_capacity(wanted, self._running())
        self.docker.start(wanted, timeout=self.limits.start_timeout)

    def stop(self, pack_id: str) -> None:
        self.docker.stop(self.spec(pack_id), timeout=self.limits.stop_timeout)

    def status(self, pack_id: str) -> str:
        return self.docker.status(self.spec(pack_id))

    def logs(self, pack_id: str, *, lines: int = 100, follow: bool = False) -> None:
        self.docker.logs(self.spec(pack_id), lines=lines, follow=follow)

    def console(self, pack_id: str, command: str) -> None:
        if not command.strip():
            raise ModpackError("控制台命令不能为空")
        self.docker.console(self.spec(pack_id), command)

    def backup(self, pack_id: str) -> Path:
        spec = self.spec(pack_id)
        running = self.docker.running(spec)
        if running:
            self.docker.console(spec, "save-off\nsave-all flush")
            time.sleep(spec.backup_quiesce_seconds)
        try:
            return create_backup(spec)
        finally:
            if running:
                self.docker.console(spec, "save-on")

    def restore(self, pack_id: str, name: str | None = None) -> Path:
        spec = self.spec(pack_id)
        if self.docker.running(spec):
            raise ModpackError("恢复前必须先停止整合包")
        source = resolve_backup(spec, name)
        if (spec.directory / "data").exists():
            create_backup(spec, prune=False)
        restored = restore_backup(spec, source.name)
        return restored

    def remove(self, pack_id: str, *, force: bool = False) -> None:
        spec = self.spec(pack_id)
        if self.docker.running(spec):
            raise ModpackError("删除前必须先停止整合包")
        protected = []
        for directory_name in ("data", "backups"):
            directory = spec.directory / directory_name
            if directory.is_dir() and any(path.is_file() and path.name != ".gitkeep" for path in directory.rglob("*")):
                protected.append(directory_name)
        if protected and not force:
            raise ModpackError(f"整合包含有 {'/'.join(protected)} 数据；确认后使用 --force")
        self.docker.remove_project(spec)
        shutil.rmtree(spec.directory)

    def stop_all(self) -> tuple[list[str], list[tuple[str, str]]]:
        stopped: list[str] = []
        failed: list[tuple[str, str]] = []
        for spec in self.specs():
            try:
                if self.docker.running(spec):
                    self.docker.stop(spec, timeout=self.limits.stop_timeout)
                    stopped.append(spec.pack_id)
            except Exception as exc:  # Packs are independent; collect failures after trying all.
                failed.append((spec.pack_id, str(exc)))
        return stopped, failed
