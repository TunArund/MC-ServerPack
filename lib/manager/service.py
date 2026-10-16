"""Terminal-facing facade for all Manager operations."""

from __future__ import annotations

from pathlib import Path

from .. import remote
from ..config import CONFIG_FILE, AppConfig, load, load_env
from ..power import InstanceStatus, Power, PowerError, PowerTarget, create_power
from ..remote import CommandError
from .deploy import ManagerDeployment
from .modpacks import ModpackError, ModpackService, validate_pack_id


class ManagerService:
    def __init__(
        self, config_path: Path = CONFIG_FILE, *, config: AppConfig | None = None,
        transport: remote.Transport | None = None,
    ) -> None:
        self.config_path = config_path
        self.config = config or load(config_path)
        self.transport = transport or remote.Transport()
        self.deployment = ManagerDeployment(
            config_path, config=self.config, transport=self.transport,
        )
        self.modpacks = ModpackService(self.config, transport=self.transport)

    def check(self) -> None:
        self.deployment.check()

    def deploy(self) -> None:
        self.deployment.deploy()

    def power(self) -> Power:
        settings = self.config.manager.power_api
        if not settings:
            raise PowerError("manager.power-api 未配置")
        return create_power(
            settings.provider,
            PowerTarget(settings.instance_id, settings.instance_region),
            load_env(self.config_path.parent / ".env"),
        )

    def power_action(self, action: str, *, force: bool = False) -> InstanceStatus:
        power = self.power()
        if action == "status":
            return power.status()
        if action == "start":
            return power.start()
        if action != "stop":
            raise PowerError(f"未知电源操作：{action}")
        current = power.status()
        if current.state == "RUNNING":
            try:
                self.modpacks.remote_manager(["stop-all"])
            except (CommandError, ModpackError):
                if not force:
                    raise
        return power.stop(current)

    def deploy_pack(self, pack_id: str, *, force: bool = False) -> None:
        self.modpacks.deploy_pack(pack_id, force=force)

    def create_pack(self, pack_id: str) -> Path:
        return self.modpacks.create_pack(pack_id)

    def remote_pack(
        self, action: str, pack_id: str | None = None, *,
        extra: tuple[str, ...] = (), tty: bool = False,
    ) -> None:
        arguments = [action]
        if pack_id is not None:
            arguments.append(validate_pack_id(pack_id))
        arguments.extend(extra)
        self.modpacks.remote_manager(arguments, tty=tty)
