"""Application service for the manager's modpack lifecycle."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from .config import MANAGER_ROOT, PACKS_DIR, PackSpec, load_pack, render_compose, validate_pack_id
from .docker import DockerClient


class ModpackError(RuntimeError):
    pass


class ModpackManager:
    def __init__(self, manager_root: Path = MANAGER_ROOT, docker: DockerClient | None = None) -> None:
        self.manager_root = manager_root
        self.packs_dir = manager_root / "modpacks" if manager_root != MANAGER_ROOT else PACKS_DIR
        self.docker = docker or DockerClient(manager_root)

    def spec(self, pack_id: str) -> PackSpec:
        return load_pack(pack_id, self.packs_dir)

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

    def start(self, pack_id: str) -> None:
        self.docker.start(self.spec(pack_id))

    def stop(self, pack_id: str) -> None:
        self.docker.stop(self.spec(pack_id))

    def status(self, pack_id: str) -> str:
        return self.docker.status(self.spec(pack_id))

    def logs(self, pack_id: str, *, lines: int = 100, follow: bool = False) -> None:
        self.docker.logs(self.spec(pack_id), lines=lines, follow=follow)

    def console(self, pack_id: str, command: str) -> None:
        if not command.strip():
            raise ModpackError("控制台命令不能为空")
        self.docker.console(self.spec(pack_id), command)

    def stop_all(self) -> tuple[list[str], list[tuple[str, str]]]:
        stopped: list[str] = []
        failed: list[tuple[str, str]] = []
        for config_file in sorted(self.packs_dir.glob("*/config.yml")):
            pack_id = config_file.parent.name
            if pack_id == "template":
                continue
            try:
                spec = self.spec(pack_id)
                if self.docker.running(spec):
                    self.docker.stop(spec)
                    stopped.append(pack_id)
            except Exception as exc:  # Packs are independent; collect failures after trying all.
                failed.append((pack_id, str(exc)))
        return stopped, failed
