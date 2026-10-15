"""Terminal-side deployment and remote Manager orchestration."""

from __future__ import annotations

import re
import shutil
import tempfile
from pathlib import Path

from . import remote
from .cloud import CloudTarget, TencentCloud
from .config import CONFIG_FILE, ROOT, AppConfig, Endpoint, load, render_runtime_section


MANAGER_SOURCE = ROOT / "manager"
MONITOR_SOURCE = ROOT / "monitor"
ENV_FILE = ROOT / ".env"
PACK_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

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


class TerminalError(RuntimeError):
    pass


def load_env(path: Path = ENV_FILE) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise TerminalError(f"缺少 {path}；请从 .env.example 复制") from exc
    values: dict[str, str] = {}
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("\"'")
    return values


class Transport:
    def ssh(self, target: str, arguments: list[str], **kwargs):
        return remote.ssh(target, arguments, **kwargs)

    def scp(self, source: Path, destination: str) -> None:
        remote.scp(source, destination)

    def archive(self, source: Path, exclude: set[str] | None = None) -> Path:
        return remote.archive_tree(source, exclude=exclude)

    def cleanup(self, archive: Path) -> None:
        remote.cleanup_archive(archive)


class TerminalService:
    def __init__(
        self, config_path: Path = CONFIG_FILE, *, transport: Transport | None = None,
        cloud_factory=TencentCloud,
    ) -> None:
        self.config_path = config_path
        self.env_path = config_path.parent / ".env"
        self.config: AppConfig = load(config_path)
        self.transport = transport or Transport()
        self.cloud_factory = cloud_factory

    @staticmethod
    def _destination(endpoint: Endpoint) -> str:
        return remote.target(endpoint.user, endpoint.host)

    def _endpoint(self, runtime: str) -> Endpoint:
        if runtime == "manager":
            return self.config.manager.endpoint
        if runtime == "monitor" and self.config.monitor.enabled and self.config.monitor.endpoint:
            return self.config.monitor.endpoint
        raise TerminalError("Monitor 未启用或连接配置不完整")

    def check(self, runtime: str) -> None:
        if runtime == "manager" and not (MANAGER_SOURCE / "manager.sh").is_file():
            raise TerminalError("缺少 Manager 部署文件")
        if runtime == "monitor":
            required = (MONITOR_SOURCE / "compose.yml", MONITOR_SOURCE / "Dockerfile", MONITOR_SOURCE / "src/app.py")
            if any(not path.is_file() or path.stat().st_size == 0 for path in required):
                raise TerminalError("Monitor 本体尚未实现")
        endpoint = self._endpoint(runtime)
        destination = self._destination(endpoint)
        self.transport.ssh(destination, ["true"], capture=True)
        check = (
            "python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 10))' && "
            "command -v crontab && command -v docker && docker compose version && docker info"
            if runtime == "manager" else
            "command -v docker && docker compose version && docker info"
        )
        self.transport.ssh(
            destination, ["bash", "-lc", check],
            capture=True,
        )

    def _upload_text(self, text: str, destination: str, mode: int = 0o644) -> None:
        with tempfile.TemporaryDirectory(prefix="mcsp-config-") as directory:
            output = Path(directory) / "payload"
            output.write_text(text, encoding="utf-8")
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
        values = load_env(self.env_path)
        keys = ("POWER_SECRET_ID", "POWER_SECRET_KEY")
        if any(not values.get(key) for key in keys):
            raise TerminalError("Manager 自动关机需要 POWER_SECRET_ID/POWER_SECRET_KEY")
        return "".join(f"{key}={values[key]}\n" for key in keys)

    def deploy_manager(self) -> None:
        endpoint = self.config.manager.endpoint
        destination = self._destination(endpoint)
        self.transport.ssh(
            destination, ["bash", "-s", "--", "manager"], input_text=BOOTSTRAP,
        )
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
            raise TerminalError("Monitor 本体尚未实现，拒绝部署空壳")
        endpoint = self._endpoint("monitor")
        destination = self._destination(endpoint)
        self.transport.ssh(destination, ["bash", "-s", "--", "monitor"], input_text=BOOTSTRAP)
        self._sync_source(MONITOR_SOURCE, endpoint)
        self._upload_text(
            render_runtime_section(self.config_path, "monitor"),
            f"{destination}:{endpoint.directory}/config.yml",
        )
        values = load_env(self.env_path)
        keys = ("POWER_SECRET_ID", "POWER_SECRET_KEY", "QQBOT_APP_ID", "QQBOT_APP_KEY")
        content = "".join(f"{key}={values.get(key, '')}\n" for key in keys)
        self._upload_text(content, f"{destination}:{endpoint.directory}/.env", 0o600)
        self.transport.ssh(destination, ["chmod", "600", endpoint.directory + "/.env"])
        self.transport.ssh(destination, [
            "bash", "-lc", f"cd {remote.shell_join([endpoint.directory])} && docker compose up -d --build",
        ])

    def remote_manager(self, arguments: list[str], *, tty: bool = False) -> None:
        endpoint = self.config.manager.endpoint
        command = f"cd {remote.shell_join([endpoint.directory])} && exec ./manager.sh {remote.shell_join(arguments)}"
        self.transport.ssh(
            self._destination(endpoint), ["bash", "-lc", command], tty=tty,
        )

    def deploy_pack(self, pack_id: str, *, force: bool = False) -> None:
        if not PACK_ID_RE.fullmatch(pack_id) or pack_id == "template":
            raise TerminalError(f"无效整合包 ID：{pack_id}")
        local = MANAGER_SOURCE / "modpacks" / pack_id
        if not (local / "src").is_dir() or not (local / "config.yml").is_file():
            raise TerminalError(f"本地整合包不存在：{pack_id}")
        endpoint = self.config.manager.endpoint
        destination = self._destination(endpoint)
        remote_pack = f"{endpoint.directory}/modpacks/{pack_id}"
        exists = self.transport.ssh(
            destination, ["test", "-e", remote_pack], capture=True, check=False,
        )
        if exists.returncode == 0 and not force:
            raise TerminalError(f"远端整合包已存在：{pack_id}；确认覆盖请使用 --force")
        if exists.returncode not in (0, 1):
            raise TerminalError("无法检查远端整合包")
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
        if not PACK_ID_RE.fullmatch(pack_id) or pack_id == "template":
            raise TerminalError(f"无效整合包 ID：{pack_id}")
        template = MANAGER_SOURCE / "modpacks/template"
        target = MANAGER_SOURCE / "modpacks" / pack_id
        if target.exists():
            raise TerminalError(f"本地整合包已存在：{pack_id}")
        shutil.copytree(template, target, ignore=shutil.ignore_patterns("compose.yml", "__pycache__"))
        config_file = target / "config.yml"
        content = config_file.read_text(encoding="utf-8")
        config_file.write_text(
            re.sub(r"(?m)^name:\s*.*$", f"name: {pack_id}", content, count=1),
            encoding="utf-8",
        )
        return target

    def cloud(self) -> TencentCloud:
        manager = self.config.manager
        if not manager.region or not manager.instance_id:
            raise TerminalError("manager.region 和 manager.instance-id 未配置")
        values = load_env(self.env_path)
        secret_id = values.get("POWER_SECRET_ID", "")
        secret_key = values.get("POWER_SECRET_KEY", "")
        if not secret_id or not secret_key:
            raise TerminalError("缺少 POWER_SECRET_ID/POWER_SECRET_KEY")
        return self.cloud_factory(CloudTarget(
            manager.region, manager.instance_id, manager.endpoint.directory,
            manager.endpoint.user,
        ), secret_id, secret_key)
