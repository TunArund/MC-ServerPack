"""Local preparation and remote Manager operations for modpacks."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from . import remote
from .config import ROOT, AppConfig


MANAGER_SOURCE = ROOT / "manager"
PACK_ID_RE = re.compile(r"[A-Za-z0-9-]+")


class ModpackError(RuntimeError):
    pass


def validate_pack_id(pack_id: str) -> str:
    if not isinstance(pack_id, str) or not PACK_ID_RE.fullmatch(pack_id) or pack_id == "template":
        raise ModpackError(f"无效整合包 ID：{pack_id}")
    return pack_id


class ModpackService:
    def __init__(self, config: AppConfig, *, transport: remote.Transport | None = None) -> None:
        self.config = config
        self.transport = transport or remote.Transport()

    def remote_manager(self, arguments: list[str], *, tty: bool = False) -> None:
        endpoint = self.config.manager.endpoint
        command = f"cd {remote.shell_join([endpoint.directory])} && exec ./manager.sh {remote.shell_join(arguments)}"
        self.transport.ssh(
            remote.target(endpoint.user, endpoint.host), ["bash", "-lc", command], tty=tty,
        )

    def deploy_pack(self, pack_id: str, *, force: bool = False) -> None:
        validate_pack_id(pack_id)
        local = MANAGER_SOURCE / "modpacks" / pack_id
        if not (local / "src").is_dir() or not (local / "config.yml").is_file():
            raise ModpackError(f"本地整合包不存在：{pack_id}")
        endpoint = self.config.manager.endpoint
        destination = remote.target(endpoint.user, endpoint.host)
        remote_pack = f"{endpoint.directory}/modpacks/{pack_id}"
        exists = self.transport.ssh(
            destination, ["test", "-e", remote_pack], capture=True, check=False,
        )
        if exists.returncode == 0 and not force:
            raise ModpackError(f"远端整合包已存在：{pack_id}；确认覆盖请使用 --force")
        if exists.returncode not in (0, 1):
            raise ModpackError("无法检查远端整合包")
        archive = self.transport.archive(local / "src")
        remote_archive = remote_pack + "/.src-upload.tar.gz"
        try:
            self.transport.ssh(destination, ["mkdir", "-p", remote_pack, remote_pack + "/data", remote_pack + "/backups"])
            self.transport.scp(archive, f"{destination}:{remote_archive}")
            script = (
                f"rm -rf -- {remote.shell_join([remote_pack + '/src'])}; "
                f"mkdir -p {remote.shell_join([remote_pack + '/src'])}; "
                f"tar -xzf {remote.shell_join([remote_archive])} -C {remote.shell_join([remote_pack + '/src'])}; "
                f"rm -f -- {remote.shell_join([remote_archive])}"
            )
            self.transport.ssh(destination, ["bash", "-lc", script])
            self.transport.scp(local / "config.yml", f"{destination}:{remote_pack}/config.yml")
        finally:
            self.transport.cleanup(archive)
        self.remote_manager(["config", pack_id])

    def create_pack(self, pack_id: str) -> Path:
        validate_pack_id(pack_id)
        template = MANAGER_SOURCE / "modpacks/template"
        target = MANAGER_SOURCE / "modpacks" / pack_id
        if target.exists():
            raise ModpackError(f"本地整合包已存在：{pack_id}")
        shutil.copytree(template, target, ignore=shutil.ignore_patterns("compose.yml", "__pycache__"))
        config_file = target / "config.yml"
        content = config_file.read_text(encoding="utf-8")
        config_file.write_text(
            re.sub(r"(?m)^name:\s*.*$", f"name: {pack_id}", content, count=1),
            encoding="utf-8",
        )
        return target
