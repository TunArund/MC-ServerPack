"""Deploy Manager and Monitor runtimes to their independent hosts."""

from __future__ import annotations

import tempfile
from pathlib import Path

from . import remote
from .config import (
    CONFIG_FILE, ROOT, AppConfig, Endpoint, load, load_env,
    render_runtime_section,
)


MANAGER_SOURCE = ROOT / "manager"
MONITOR_SOURCE = ROOT / "monitor"
BOOTSTRAP = r"""set -Eeuo pipefail
role="$1"
if [ "$(id -u)" -eq 0 ]; then run_root() { "$@"; }; else run_root() { sudo -n "$@"; }; fi
need=0
command -v python3 >/dev/null 2>&1 || need=1
python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 10))' >/dev/null 2>&1 || need=1
if [ "$role" = manager ]; then command -v crontab >/dev/null 2>&1 || need=1; fi
command -v docker >/dev/null 2>&1 || need=1
docker compose version >/dev/null 2>&1 || need=1
if [ "$need" = 1 ]; then
  [ "$(id -u)" -eq 0 ] || sudo -n true >/dev/null 2>&1 || { echo '安装环境需要免密 sudo' >&2; exit 1; }
  . /etc/os-release
  case "$ID" in debian|ubuntu) ;; *) echo "暂不支持自动安装：$ID" >&2; exit 1 ;; esac
  run_root apt-get update
  run_root env DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-venv python3-pip cron ca-certificates curl
  if ! docker compose version >/dev/null 2>&1; then
    run_root install -m 0755 -d /etc/apt/keyrings
    curl -fsSL "https://download.docker.com/linux/$ID/gpg" -o /tmp/mcsp-docker.asc
    run_root install -m 0644 /tmp/mcsp-docker.asc /etc/apt/keyrings/docker.asc
    rm -f /tmp/mcsp-docker.asc
    arch="$(dpkg --print-architecture)"
    repo="deb [arch=$arch signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/$ID $VERSION_CODENAME stable"
    run_root sh -c "printf '%s\\n' '$repo' > /etc/apt/sources.list.d/docker.list"
    run_root apt-get update
    run_root env DEBIAN_FRONTEND=noninteractive apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  fi
  run_root systemctl enable --now docker
  [ "$role" != manager ] || run_root systemctl enable --now cron
fi
if [ "$(id -u)" -ne 0 ] && ! id -nG | tr ' ' '\n' | grep -qx docker; then
  run_root usermod -aG docker "$(id -un)"
  echo '已加入 docker 组，请重新登录后再次部署' >&2
  exit 3
fi
docker info >/dev/null
"""


class DeployError(RuntimeError):
    pass


class DeploymentService:
    def __init__(
        self, config_path: Path = CONFIG_FILE, *, config: AppConfig | None = None,
        transport: remote.Transport | None = None,
    ) -> None:
        self.config_path = config_path
        self.env_path = config_path.parent / ".env"
        self.config = config or load(config_path)
        self.transport = transport or remote.Transport()

    @staticmethod
    def _destination(endpoint: Endpoint) -> str:
        return remote.target(endpoint.user, endpoint.host)

    def _endpoint(self, runtime: str) -> Endpoint:
        if runtime == "manager":
            return self.config.manager.endpoint
        if runtime == "monitor" and self.config.monitor.enabled and self.config.monitor.endpoint:
            return self.config.monitor.endpoint
        raise DeployError("Monitor 未启用或连接配置不完整")

    def check(self, runtime: str) -> None:
        if runtime == "manager" and not (MANAGER_SOURCE / "manager.sh").is_file():
            raise DeployError("缺少 Manager 部署文件")
        if runtime == "monitor":
            required = (MONITOR_SOURCE / "compose.yml", MONITOR_SOURCE / "Dockerfile", MONITOR_SOURCE / "src/app.py")
            if any(not path.is_file() or path.stat().st_size == 0 for path in required):
                raise DeployError("Monitor 本体尚未实现")
        endpoint = self._endpoint(runtime)
        destination = self._destination(endpoint)
        self.transport.ssh(destination, ["true"], capture=True)
        check = (
            "python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 10))' && "
            "command -v crontab && command -v docker && docker compose version && docker info"
            if runtime == "manager" else
            "command -v docker && docker compose version && docker info"
        )
        self.transport.ssh(destination, ["bash", "-lc", check], capture=True)

    def _upload_text(self, content: str, destination: str, mode: int = 0o644) -> None:
        with tempfile.TemporaryDirectory(prefix="mcsp-config-") as directory:
            output = Path(directory) / "payload"
            output.write_text(content, encoding="utf-8")
            output.chmod(mode)
            self.transport.scp(output, destination)

    def _sync_source(self, source: Path, endpoint: Endpoint) -> None:
        destination = self._destination(endpoint)
        directory = endpoint.directory
        excluded = {
            "config.yml", ".env", ".venv", "data",
            ".schedule-state.json", "schedule.log", "deploy.log",
        }
        packs = source / "modpacks"
        if packs.is_dir():
            excluded.update(
                f"modpacks/{path.name}" for path in packs.iterdir()
                if path.name != "template"
            )
        archive = self.transport.archive(source, excluded)
        remote_archive = directory + "/.mcsp-upload.tar.gz"
        try:
            self.transport.ssh(destination, ["mkdir", "-p", directory])
            self.transport.scp(archive, f"{destination}:{remote_archive}")
            names = ["lib", "runtime", "src", "manager.sh", "gateway.sh", "Dockerfile", "compose.yml", "requirements.txt"]
            targets = [directory + "/" + name for name in names]
            script = (
                "rm -rf -- " + remote.shell_join(targets) + "; "
                f"tar -xzf {remote.shell_join([remote_archive])} -C {remote.shell_join([directory])}; "
                f"rm -f -- {remote.shell_join([remote_archive])}"
            )
            self.transport.ssh(destination, ["bash", "-lc", script])
        finally:
            self.transport.cleanup(archive)

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

    def deploy_manager(self) -> None:
        endpoint = self.config.manager.endpoint
        destination = self._destination(endpoint)
        self.transport.ssh(destination, ["bash", "-s", "--", "manager"], input_text=BOOTSTRAP)
        self._sync_source(MANAGER_SOURCE, endpoint)
        self._upload_text(
            render_runtime_section(self.config_path, "manager"),
            f"{destination}:{endpoint.directory}/config.yml",
        )
        environment = self._manager_env()
        if environment:
            self._upload_text(environment, f"{destination}:{endpoint.directory}/.env", 0o600)
            self.transport.ssh(destination, ["chmod", "600", endpoint.directory + "/.env"])
        else:
            self.transport.ssh(destination, ["rm", "-f", endpoint.directory + "/.env"])
        command = (
            f"cd {remote.shell_join([endpoint.directory])} && "
            "python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt && "
            "chmod 700 manager.sh && ./manager.sh schedule install"
        )
        self.transport.ssh(destination, ["bash", "-lc", command])

    def deploy_monitor(self) -> None:
        required = (MONITOR_SOURCE / "compose.yml", MONITOR_SOURCE / "Dockerfile", MONITOR_SOURCE / "src/app.py")
        if any(not path.is_file() or path.stat().st_size == 0 for path in required):
            raise DeployError("Monitor 本体尚未实现，拒绝部署空壳")
        endpoint = self._endpoint("monitor")
        destination = self._destination(endpoint)
        self.transport.ssh(destination, ["bash", "-s", "--", "monitor"], input_text=BOOTSTRAP)
        self._sync_source(MONITOR_SOURCE, endpoint)
        self._upload_text(
            render_runtime_section(self.config_path, "monitor"),
            f"{destination}:{endpoint.directory}/config.yml",
        )
        values = load_env(self.env_path)
        power = self.config.monitor.power_api
        power_keys = (
            ("POWER_SECRET_ID", "POWER_SECRET_KEY")
            if power and power.provider == "tencent" else ("POWER_SECRET_KEY",)
        )
        if any(not values.get(key) for key in power_keys):
            raise DeployError(f"Monitor 电源 API 需要 {'/'.join(power_keys)}")
        keys = ("POWER_SECRET_ID", "POWER_SECRET_KEY", "QQBOT_APP_ID", "QQBOT_APP_KEY")
        content = "".join(f"{key}={values.get(key, '')}\n" for key in keys)
        self._upload_text(content, f"{destination}:{endpoint.directory}/.env", 0o600)
        self.transport.ssh(destination, ["chmod", "600", endpoint.directory + "/.env"])
        self.transport.ssh(destination, [
            "bash", "-lc", f"cd {remote.shell_join([endpoint.directory])} && docker compose up -d --build",
        ])
