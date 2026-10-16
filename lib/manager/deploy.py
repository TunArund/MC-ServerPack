"""Deploy the Manager runtime and install its aggregate schedule."""

from __future__ import annotations

from pathlib import Path

from .. import remote
from ..config import CONFIG_FILE, ROOT, AppConfig, load, load_env
from ..deploy import DeployError, RuntimeDeployment


class ManagerDeployment(RuntimeDeployment):
    runtime = "manager"
    source = ROOT / "manager"

    def __init__(
        self, config_path: Path = CONFIG_FILE, *, config: AppConfig | None = None,
        transport: remote.Transport | None = None,
    ) -> None:
        loaded = config or load(config_path)
        super().__init__(
            loaded.manager.endpoint, config_path,
            config=loaded, transport=transport,
        )

    def check(self) -> None:
        self.check_environment(
            (self.source / "manager.sh",),
            "python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 10))' "
            "&& command -v crontab && command -v docker "
            "&& docker compose version && docker info",
        )

    def _manager_env(self) -> str:
        if not self.config.manager.auto_stop:
            return ""
        power = self.config.manager.power_api
        if not power:
            raise DeployError("Manager 自动关机需要 power-api")
        values = load_env(self.env_path)
        required = (
            ("POWER_SECRET_ID", "POWER_SECRET_KEY")
            if power.provider == "tencent" else ("POWER_SECRET_KEY",)
        )
        if any(not values.get(key) for key in required):
            raise DeployError(f"Manager 自动关机需要 {'/'.join(required)}")
        keys = ("POWER_SECRET_ID", "POWER_SECRET_KEY")
        return "".join(f"{key}={values.get(key, '')}\n" for key in keys)

    def deploy(self) -> None:
        packs = self.source / "modpacks"
        excluded = {
            f"modpacks/{path.name}" for path in packs.iterdir()
            if path.name != "template"
        } if packs.is_dir() else set()
        self.prepare(excluded=excluded)
        environment = self._manager_env()
        if environment:
            self.upload_text(
                environment,
                f"{self.destination}:{self.endpoint.directory}/.env", 0o600,
            )
            self.transport.ssh(
                self.destination,
                ["chmod", "600", self.endpoint.directory + "/.env"],
            )
        else:
            self.transport.ssh(
                self.destination, ["rm", "-f", self.endpoint.directory + "/.env"],
            )
        command = (
            f"cd {remote.shell_join([self.endpoint.directory])} && "
            "python3 -m venv .venv && "
            "./.venv/bin/pip install -r lib/requirements.txt && "
            "chmod 700 manager.sh && ./manager.sh schedule install"
        )
        self.transport.ssh(self.destination, ["bash", "-lc", command])
