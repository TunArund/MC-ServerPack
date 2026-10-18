"""Terminal 对 Manager 公共命令接口的薄封装。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional, Tuple

from . import remote
from .config import (
    CONFIG_FILE, ROOT, ManagerConfig, load_env, load_manager,
    render_runtime_section,
)
from .deploy import RuntimeDeployment
from .power import Power, PowerError, PowerTarget, create_power


class ManagerError(remote.CommandError):
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
            preserve=(
                "modpacks", ".venv", "tmp", "manager.log", "schedule.log",
                ".schedule-state.json",
            ),
            transport=self.transport,
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
        remote_command = ["bash", "-lc", command]
        if capture:
            return self.transport.ssh(
                self.destination, remote_command, tty=tty, capture=True,
            )
        return self.transport.ssh_tail(
            self.destination, remote_command,
            label=f"[Manager] {' '.join(arguments[:2])}", tty=tty,
        )

    def local(self, arguments: list[str], *, capture: bool = False):
        return remote.run(
            [str(ROOT / "manager/manager.sh"), *arguments], capture=capture,
        )

    def deploy_modpack(self, pack_id: str) -> None:
        print(f"[整合包 1/3] 验证本地整合包：{pack_id}", flush=True)
        if not re.fullmatch(r"[A-Za-z0-9-]+", pack_id):
            raise ManagerError("整合包 ID 仅允许大小写字母、数字和减号")
        local = ROOT / "manager/modpacks" / pack_id
        if not (local / "src").is_dir() or not (local / "config.yml").is_file():
            raise ManagerError(f"本地整合包不存在或不完整：{pack_id}")
        self.local(["config", pack_id], capture=True)
        staging = self.endpoint.directory + f"/tmp/transfers/{pack_id}"
        print(
            f"[整合包 2/3] 增量同步到 {self.destination}:{staging}",
            flush=True,
        )
        uploaded, deleted = self.transport.sync_tree(
            local, self.destination, staging,
            exclude=("data", "data/*", "backups", "backups/*"),
        )
        print(
            f"[整合包 2/3] 增量同步完成：上传 {uploaded}，删除 {deleted}",
            flush=True,
        )
        arguments = ["import-dir", pack_id, staging]
        print("[整合包 3/3] 由远端 Manager 验证并导入", flush=True)
        self.command(arguments)

    def run(
        self, action: str, pack_id: Optional[str] = None, *,
        extra: Tuple[str, ...] = (), tty: bool = False,
    ) -> None:
        arguments = [action]
        if pack_id is not None:
            arguments.append(pack_id)
        arguments.extend(extra)
        if action == "list":
            result = self.command(arguments, capture=True)
            raw = result.stdout or b""
            output = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
            if output:
                print(output, end="" if output.endswith("\n") else "\n")
            return
        self.command(arguments, tty=tty)
