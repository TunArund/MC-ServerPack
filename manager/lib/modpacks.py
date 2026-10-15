"""Application service for the manager's modpack lifecycle."""

from __future__ import annotations

import re
import shutil
import time
from pathlib import Path

from .archive import create_backup, resolve_backup, restore_backup
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

    def create(self, pack_id: str) -> Path:
        validate_pack_id(pack_id)
        target = self.packs_dir / pack_id
        if target.exists():
            raise ModpackError(f"整合包已存在：{pack_id}")
        template = self.packs_dir / "template"
        if not template.is_dir():
            raise ModpackError("缺少 modpacks/template")
        shutil.copytree(template, target, ignore=shutil.ignore_patterns("compose.yml", "__pycache__"))
        config_file = target / "config.yml"
        content = config_file.read_text(encoding="utf-8")
        content = re.sub(r"(?m)^name:\s*.*$", f"name: {pack_id}", content, count=1)
        config_file.write_text(content, encoding="utf-8")
        for name in ("src", "data", "backups"):
            (target / name).mkdir(exist_ok=True)
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
