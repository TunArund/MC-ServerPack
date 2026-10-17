"""Terminal 对 Manager 公共命令接口的薄封装。"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Optional, Tuple

from . import remote
from .config import (
    CONFIG_FILE, ROOT, ManagerConfig, load_env, load_manager,
    render_runtime_section,
)
from .deploy import RuntimeDeployment
from .power import Power, PowerError, PowerTarget, create_power


class ManagerError(RuntimeError):
    """Manager 未遵循约定的公共命令协议。"""


class Manager:
    """调用 Manager 公共入口，不实现整合包和容器业务。"""

    def __init__(
        self, config_path: Path = CONFIG_FILE, *, config: Optional[ManagerConfig] = None,
        transport: Optional[remote.Transport] = None,
    ) -> None:
        self.config_path = config_path
        self.config = config or load_manager(config_path)
        self.transport = transport or remote.Transport()
        self.endpoint = self.config.endpoint
        self.deployment = RuntimeDeployment(
            ROOT / "manager", self.endpoint, "manager.sh",
            preserve=("modpacks", ".venv"), transport=self.transport,
        )

    @property
    def destination(self) -> str:
        return remote.target(self.endpoint.user, self.endpoint.host)

    def check(self) -> None:
        self.deployment.check()

    def install(self) -> None:
        self.deployment.install(
            render_runtime_section(self.config_path, "manager"), self._environment(),
        )

    def publish(self) -> None:
        self.deployment.publish()

    def configure(self) -> None:
        self.deployment.configure(
            render_runtime_section(self.config_path, "manager"),
            self._environment(),
        )

    def _environment(self) -> str:
        values = load_env(self.config_path.parent / ".env")
        return "".join(
            f"{key}={values.get(key, '')}\n"
            for key in ("POWER_SECRET_ID", "POWER_SECRET_KEY")
            if key in values
        )

    def power(self) -> Power:
        settings = self.config.power_api
        if not settings:
            raise PowerError("manager.power-api 未配置")
        return create_power(
            settings.provider,
            PowerTarget(settings.instance_id, settings.instance_region),
            load_env(self.config_path.parent / ".env"),
        )

    def command(
        self, arguments: list[str], *, tty: bool = False, capture: bool = False,
    ):
        command = (
            f"cd {remote.shell_join([self.endpoint.directory])} && "
            f"exec ./manager.sh {remote.shell_join(arguments)}"
        )
        return self.transport.ssh(
            self.destination, ["bash", "-lc", command], tty=tty, capture=capture,
        )

    def local(self, arguments: list[str], *, capture: bool = False):
        return remote.run(
            [str(ROOT / "manager/manager.sh"), *arguments], capture=capture,
        )

    def export(self, pack_id: str) -> Path:
        result = self.local(["export", pack_id], capture=True)
        output = result.stdout.decode("utf-8", "replace")
        match = re.search(r"^archive:\s*(\S+)\s*$", output, re.MULTILINE)
        if not match:
            raise ManagerError("Manager 未返回整合包归档")
        return Path(match.group(1))

    def deploy_modpack(self, pack_id: str, *, force: bool = False) -> None:
        archive = self.export(pack_id)
        remote_archive = self.endpoint.directory + "/.mcsp-pack-upload.tar.gz"
        try:
            self.transport.scp(archive, f"{self.destination}:{remote_archive}")
            arguments = ["import", pack_id, remote_archive]
            if force:
                arguments.append("--force")
            self.command(arguments)
        finally:
            shutil.rmtree(archive.parent, ignore_errors=True)
            self.transport.ssh(
                self.destination, ["rm", "-f", remote_archive], check=False,
            )

    def run(
        self, action: str, pack_id: Optional[str] = None, *,
        extra: Tuple[str, ...] = (), tty: bool = False,
    ) -> None:
        arguments = [action]
        if pack_id is not None:
            arguments.append(pack_id)
        arguments.extend(extra)
        self.command(arguments, tty=tty)
