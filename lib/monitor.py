"""Terminal 对 Monitor 公共命令接口的薄封装。"""

from __future__ import annotations

import re
import secrets
from pathlib import Path
from typing import Optional

from . import remote
from .config import (
    CONFIG_FILE, ROOT, MonitorConfig, load_env, load_monitor,
    render_runtime_section, upsert_monitor_user_target,
)
from .deploy import DeployError, RuntimeDeployment


class Monitor:
    """调用 Monitor 公共入口，不实现 Docker 或 QQ Bot 业务。"""

    def __init__(
        self, config_path: Path = CONFIG_FILE, *, config: Optional[MonitorConfig] = None,
        transport: Optional[remote.Transport] = None,
    ) -> None:
        self.config_path = config_path
        self.config = config or load_monitor(config_path)
        self.transport = transport or remote.Transport()
        if not self.config.enabled or self.config.endpoint is None:
            raise DeployError("Monitor 未启用或连接配置不完整")
        self.endpoint = self.config.endpoint
        self.deployment = RuntimeDeployment(
            ROOT / "monitor", self.endpoint, "monitor.sh",
            preserve=("data", "monitor.log"), transport=self.transport,
        )

    @property
    def destination(self) -> str:
        return remote.target(self.endpoint.user, self.endpoint.host)

    def check(self) -> None:
        self.deployment.check()

    def install(self) -> None:
        self.deployment.install(
            render_runtime_section(self.config_path, "monitor"), self._environment(),
        )

    def publish(self) -> None:
        self.deployment.publish()

    def configure(self) -> None:
        self.deployment.configure(
            render_runtime_section(self.config_path, "monitor"),
            self._environment(),
        )

    def _environment(self) -> str:
        values = load_env(self.config_path.parent / ".env")
        environment = "".join(
            f"{key}={values.get(key, '')}\n"
            for key in ("POWER_SECRET_ID", "POWER_SECRET_KEY")
            if key in values
        )
        qq_id = values.get("QQBOT_APP_ID")
        qq_key = values.get("QQBOT_APP_KEY")
        if qq_id is not None:
            environment += f"QQBOT_APP_ID={qq_id}\n"
        if qq_key is not None:
            environment += f"QQBOT_APP_KEY={qq_key}\n"
        return environment

    def command(self, arguments: list[str], *, capture: bool = False):
        command = (
            f"cd {remote.shell_join([self.endpoint.directory])} && "
            f"exec ./monitor.sh {remote.shell_join(arguments)}"
        )
        remote_command = ["bash", "-lc", command]
        if capture:
            return self.transport.ssh(
                self.destination, remote_command, capture=True,
            )
        return self.transport.ssh_tail(
            self.destination, remote_command,
            label=f"[Monitor] {' '.join(arguments[:2])}",
        )

    def run(self, action: str) -> None:
        self.command([action])

    def add_target(self, name: str) -> str:
        code = secrets.token_hex(2)
        print(f"请向机器人发送：{code}", flush=True)
        result = self.command(["add-target", code], capture=True)
        output = (
            result.stdout.decode("utf-8", "replace")
            if isinstance(result.stdout, bytes) else (result.stdout or "")
        )
        matches = re.findall(r"^user_openid:\s*(\S+)\s*$", output, re.MULTILINE)
        if len(matches) != 1:
            raise DeployError("远端 Monitor 未返回唯一 user_openid")
        identifier = matches[0]
        upsert_monitor_user_target(self.config_path, name, identifier)
        self.configure()
        return identifier
