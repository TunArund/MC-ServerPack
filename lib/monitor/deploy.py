"""Deploy and communicate with the independently hosted Monitor runtime."""

from __future__ import annotations

import re
from pathlib import Path

from .. import remote
from ..config import (
    CONFIG_FILE, ROOT, AppConfig, load, load_env, render_runtime_section,
)
from ..deploy import DeployError, RuntimeDeployment


class MonitorDeployment(RuntimeDeployment):
    runtime = "monitor"
    source = ROOT / "monitor"

    def __init__(
        self, config_path: Path = CONFIG_FILE, *, config: AppConfig | None = None,
        transport: remote.Transport | None = None,
    ) -> None:
        loaded = config or load(config_path)
        if not loaded.monitor.enabled or loaded.monitor.endpoint is None:
            raise DeployError("Monitor 未启用或连接配置不完整")
        super().__init__(
            loaded.monitor.endpoint, config_path,
            config=loaded, transport=transport,
        )

    @property
    def required(self) -> tuple[Path, ...]:
        return (
            self.source / "compose.yml",
            self.source / "Dockerfile",
            self.source / "src/app.py",
        )

    def check(self) -> None:
        self.check_environment(
            self.required,
            "command -v docker && docker compose version && docker info",
        )

    def deploy(self) -> None:
        if any(not path.is_file() or path.stat().st_size == 0 for path in self.required):
            raise DeployError("Monitor 本体尚未实现，拒绝部署空壳")
        values = load_env(self.env_path)
        power = self.config.monitor.power_api
        power_keys = (
            ("POWER_SECRET_ID", "POWER_SECRET_KEY")
            if power and power.provider == "tencent" else ("POWER_SECRET_KEY",)
        )
        if any(not values.get(key) for key in power_keys):
            raise DeployError(f"Monitor 电源 API 需要 {'/'.join(power_keys)}")
        self.prepare()
        keys = (
            "POWER_SECRET_ID", "POWER_SECRET_KEY",
            "QQBOT_SECRET_ID", "QQBOT_SECRET_KEY",
        )
        content = "".join(f"{key}={values.get(key, '')}\n" for key in keys)
        self.upload_text(
            content, f"{self.destination}:{self.endpoint.directory}/.env", 0o600,
        )
        self.transport.ssh(
            self.destination,
            ["chmod", "600", self.endpoint.directory + "/.env"],
        )
        command = (
            f"cd {remote.shell_join([self.endpoint.directory])} && "
            "docker compose up -d --build"
        )
        self.transport.ssh(self.destination, ["bash", "-lc", command])

    def discover_user(self, code: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{4}", code):
            raise DeployError("QQ 验证码必须是四位小写十六进制字符")
        command = (
            f"cd {remote.shell_join([self.endpoint.directory])} && "
            f"./monitor.sh add-target {remote.shell_join([code])}"
        )
        result = self.transport.ssh(
            self.destination, ["bash", "-lc", command], capture=True,
        )
        output = (
            result.stdout.decode("utf-8", "replace")
            if isinstance(result.stdout, bytes) else (result.stdout or "")
        )
        matches = re.findall(r"^user_openid:\s*(\S+)\s*$", output, re.MULTILINE)
        if len(matches) != 1:
            raise DeployError("远端 Monitor 未返回唯一 user_openid")
        return matches[0]

    def refresh_config(self) -> None:
        self.upload_text(
            render_runtime_section(self.config_path, "monitor"),
            f"{self.destination}:{self.endpoint.directory}/config.yml",
        )
        command = (
            f"cd {remote.shell_join([self.endpoint.directory])} && "
            "docker compose up -d --force-recreate monitor"
        )
        self.transport.ssh(self.destination, ["bash", "-lc", command])
