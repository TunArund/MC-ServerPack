"""Shared deployment transport for independently deployed runtimes."""

from __future__ import annotations

import tempfile
from pathlib import Path

from . import remote
from .config import CONFIG_FILE, AppConfig, Endpoint, load, render_runtime_section


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


class RuntimeDeployment:
    """Common upload mechanics; runtime-specific steps belong to subclasses."""

    runtime: str
    source: Path

    def __init__(
        self, endpoint: Endpoint, config_path: Path = CONFIG_FILE, *,
        config: AppConfig | None = None, transport: remote.Transport | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.config_path = config_path
        self.env_path = config_path.parent / ".env"
        self.config = config or load(config_path)
        self.transport = transport or remote.Transport()

    @property
    def destination(self) -> str:
        return remote.target(self.endpoint.user, self.endpoint.host)

    def check_environment(self, required: tuple[Path, ...], command: str) -> None:
        if any(not path.is_file() or path.stat().st_size == 0 for path in required):
            raise DeployError(f"{self.runtime.capitalize()} 部署文件不完整")
        self.transport.ssh(self.destination, ["true"], capture=True)
        self.transport.ssh(
            self.destination, ["bash", "-lc", command], capture=True,
        )

    def prepare(self, *, excluded: set[str] | None = None) -> None:
        self.transport.ssh(
            self.destination, ["bash", "-s", "--", self.runtime],
            input_text=BOOTSTRAP,
        )
        self.sync_source(excluded=excluded)
        self.upload_text(
            render_runtime_section(self.config_path, self.runtime),
            f"{self.destination}:{self.endpoint.directory}/config.yml",
        )

    def upload_text(self, content: str, destination: str, mode: int = 0o644) -> None:
        with tempfile.TemporaryDirectory(prefix="mcsp-config-") as directory:
            output = Path(directory) / "payload"
            output.write_text(content, encoding="utf-8")
            output.chmod(mode)
            self.transport.scp(output, destination)

    def sync_source(self, *, excluded: set[str] | None = None) -> None:
        exclusions = {
            "config.yml", ".env", ".venv", "data",
            ".schedule-state.json", "schedule.log", "deploy.log",
            *(excluded or set()),
        }
        archive = self.transport.archive(self.source, exclusions)
        remote_archive = self.endpoint.directory + "/.mcsp-upload.tar.gz"
        try:
            self.transport.ssh(
                self.destination, ["mkdir", "-p", self.endpoint.directory],
            )
            self.transport.scp(archive, f"{self.destination}:{remote_archive}")
            names = (
                "lib", "runtime", "src", "manager.sh", "monitor.sh",
                "Dockerfile", "compose.yml", "requirements.txt",
            )
            targets = [self.endpoint.directory + "/" + name for name in names]
            script = (
                "rm -rf -- " + remote.shell_join(targets) + "; "
                f"tar -xzf {remote.shell_join([remote_archive])} "
                f"-C {remote.shell_join([self.endpoint.directory])}; "
                f"rm -f -- {remote.shell_join([remote_archive])}"
            )
            self.transport.ssh(self.destination, ["bash", "-lc", script])
        finally:
            self.transport.cleanup(archive)
