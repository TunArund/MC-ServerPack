"""Terminal-facing facade for Monitor operations."""

from __future__ import annotations

import secrets
from pathlib import Path

from .. import remote
from ..config import CONFIG_FILE, AppConfig, load
from .config import upsert_user_target
from .deploy import MonitorDeployment


class MonitorService:
    def __init__(
        self, config_path: Path = CONFIG_FILE, *, config: AppConfig | None = None,
        transport: remote.Transport | None = None,
    ) -> None:
        self.config_path = config_path
        self.config = config or load(config_path)
        self.transport = transport or remote.Transport()
        self.deployment = MonitorDeployment(
            config_path, config=self.config, transport=self.transport,
        )

    def check(self) -> None:
        self.deployment.check()

    def deploy(self) -> None:
        self.deployment.deploy()

    def add_target(self, name: str) -> str:
        code = secrets.token_hex(2)
        print(f"请向机器人发送：{code}", flush=True)
        identifier = self.deployment.discover_user(code)
        upsert_user_target(self.config_path, name, identifier)
        self.deployment.refresh_config()
        return identifier
