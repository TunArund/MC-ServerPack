"""
远端账号与目录文件管理
追求幂等
"""

from __future__ import annotations

import re
import shutil
import tempfile
from pathlib import Path
from typing import Optional

from . import remote
from .config import Endpoint
from .remote import CommandError


USERNAME_RE = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
KEY_PREFIXES = ("ssh-ed25519 ", "ssh-rsa ", "ecdsa-sha2-")


class AccountError(RuntimeError):
    """部署用户参数或公钥无效。"""


def default_public_key() -> Path:
    ssh_dir = Path.home() / ".ssh"
    for name in ("id_ed25519.pub", "id_ecdsa.pub", "id_rsa.pub"):
        path = ssh_dir / name
        if path.is_file():
            return path
    raise AccountError("未找到默认 SSH 公钥；请使用 --key 指定 .pub 文件")


def read_public_key(path: Optional[Path] = None) -> str:
    source = (path or default_public_key()).expanduser().resolve()
    try:
        key = source.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise AccountError(f"无法读取 SSH 公钥 {source}：{exc}") from exc
    if "\n" in key or not key.startswith(KEY_PREFIXES):
        raise AccountError(f"SSH 公钥格式无效：{source}")
    return key


def resolve_public_key(path: Optional[Path] = None) -> Path:
    source = (path or default_public_key()).expanduser().resolve()
    if source.suffix != ".pub":
        raise AccountError(f"SSH 公钥必须是 .pub 文件：{source}")
    read_public_key(source)
    private_key = source.with_suffix("")
    if not private_key.is_file():
        raise AccountError(f"找不到 SSH 私钥：{private_key}")
    return source


class AccountProvisioner:
    """通过已有管理员 SSH 账号引导普通部署用户。"""

    def __init__(self, transport: Optional[remote.Transport] = None) -> None:
        self.transport = transport or remote.Transport()

    @staticmethod
    def _administrator(value: str) -> str:
        if not USERNAME_RE.fullmatch(value):
            raise AccountError("管理员用户名仅允许小写字母、数字、下划线和减号")
        return value

    def prepare_key(self, key_path: Optional[Path] = None) -> Path:
        return resolve_public_key(key_path)

    def test_login(self, host: str, administrator: str, key_path: Path) -> None:
        administrator = self._administrator(administrator)
        self.transport.ssh(
            remote.target(administrator, host), ["true"], capture=True,
            identity=key_path.with_suffix(""),
        )

    def create(
        self, host: str, username: str, administrator: str,
        key_path: Optional[Path] = None,
    ) -> None:
        if username == "root" or not USERNAME_RE.fullmatch(username):
            raise AccountError("用户名仅允许小写字母、数字、下划线和减号，且不能是 root")
        administrator = self._administrator(administrator)
        destination = remote.target(administrator, host)
        source = resolve_public_key(key_path)
        public_key = read_public_key(source)
        identity = source.with_suffix("")
        script = r"""set -Eeuo pipefail
user=$1
key=$2
for command in useradd getent passwd install visudo; do
  command -v "$command" >/dev/null || {
    printf '缺少系统命令：%s\n' "$command" >&2
    exit 127
  }
done
test -d /etc/sudoers.d || {
  echo '缺少 /etc/sudoers.d；请先安装 sudo' >&2
  exit 127
}
if ! id -u "$user" >/dev/null 2>&1; then
  useradd --create-home --shell /bin/bash "$user"
  passwd --lock "$user" >/dev/null
fi
home=$(getent passwd "$user" | cut -d: -f6)
group=$(id -gn "$user")
test -n "$home"
install -d -m 700 -o "$user" -g "$group" "$home/.ssh"
touch "$home/.ssh/authorized_keys"
if ! grep -qxF -- "$key" "$home/.ssh/authorized_keys"; then
  printf '%s\n' "$key" >> "$home/.ssh/authorized_keys"
fi
chown "$user:$group" "$home/.ssh/authorized_keys"
chmod 600 "$home/.ssh/authorized_keys"
sudoers="/etc/sudoers.d/$user"
temporary="$sudoers.mcsp-new"
printf '%s ALL=(ALL:ALL) NOPASSWD: ALL\n' "$user" > "$temporary"
chmod 440 "$temporary"
visudo -cf "$temporary" >/dev/null
mv -f -- "$temporary" "$sudoers"
if getent group docker >/dev/null; then
  usermod --append --groups docker "$user"
fi
printf 'user=%s home=%s\n' "$user" "$home"
"""
        command = ["bash", "-s", "--", username, public_key]
        if administrator != "root":
            command = ["sudo", "--non-interactive", *command]
        self.transport.ssh(
            destination, command, input_text=script, capture=True,
            identity=identity,
        )
        verified = self.transport.ssh(
            destination, ["getent", "passwd", username], capture=True,
            identity=identity,
        )
        output = (
            verified.stdout.decode("utf-8", "replace")
            if isinstance(verified.stdout, bytes) else (verified.stdout or "")
        )
        if not output.startswith(username + ":"):
            raise AccountError(f"远端未确认部署用户存在：{username}@{host}")


class DeployError(RuntimeError):
    pass


class DockerProvisioner:
    """Terminal 直接在目标服务器安装 Docker，不依赖运行时源码。"""

    SCRIPT = r"""set -Eeuo pipefail
STEP=初始化
trap 'code=$?; echo "Docker 安装失败（步骤：$STEP，退出码：$code）" >&2; exit "$code"' ERR

if [ "$(id -u)" -eq 0 ]; then
  SUDO=
elif command -v sudo >/dev/null 2>&1 && sudo --non-interactive true; then
  SUDO='sudo --non-interactive'
else
  echo '安装 Docker 需要 root 或免密 sudo' >&2
  exit 1
fi

as_root() {
  if [ -n "$SUDO" ]; then
    $SUDO "$@"
  else
    "$@"
  fi
}

apt_get() {
  as_root env DEBIAN_FRONTEND=noninteractive \
    apt-get -o Dpkg::Use-Pty=0 "$@"
}

configure_registry_mirrors() {
  STEP=配置DockerHub镜像加速
  echo '[Docker] 合并 Docker Hub 镜像加速配置'
  as_root install -d -m 0755 /etc/docker
  as_root python3 - /etc/docker/daemon.json <<'PY'
import json
import os
import sys
import tempfile

path = sys.argv[1]
mirrors = [
    "https://docker.1ms.run",
    "https://docker.m.daocloud.io",
    "https://dockerproxy.link",
    "https://free.hubfast.cn",
    "https://proxy.vvvv.ee",
    "https://docker.jiaxin.site",
    "https://docker.xuanyuan.me",
    "https://registry.cyou",
    "https://cf-workers-docker-io-apl.pages.dev",
]
try:
    with open(path, encoding="utf-8") as source:
        config = json.load(source)
except FileNotFoundError:
    config = {}
except (OSError, json.JSONDecodeError) as exc:
    raise SystemExit(f"无法安全合并 {path}：{exc}")
if not isinstance(config, dict):
    raise SystemExit(f"{path} 顶层必须是 JSON 对象")
existing = config.get("registry-mirrors", [])
if not isinstance(existing, list) or not all(isinstance(item, str) for item in existing):
    raise SystemExit(f"{path} 的 registry-mirrors 必须是字符串数组")
merged = list(dict.fromkeys([*existing, *mirrors]))
changed = merged != existing
config["registry-mirrors"] = merged
if "max-concurrent-downloads" not in config:
    config["max-concurrent-downloads"] = 10
    changed = True
if "max-concurrent-uploads" not in config:
    config["max-concurrent-uploads"] = 5
    changed = True
if changed:
    descriptor, temporary = tempfile.mkstemp(prefix="daemon.json.mcsp-", dir="/etc/docker")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(config, output, ensure_ascii=False, indent=2)
            output.write("\n")
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
print("changed" if changed else "unchanged")
PY
}

if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  STEP=验证现有安装
  echo '[Docker 1/2] 验证现有 Engine 与 Compose'
  as_root systemctl enable --now docker.service containerd.service
  if [ "$(id -un)" != root ]; then
    as_root usermod --append --groups docker "$(id -un)"
  fi
  mirror_state=$(configure_registry_mirrors | tee /dev/stderr | tail -n 1)
  if [ "$mirror_state" = changed ]; then
    as_root systemctl restart docker.service
  fi
  echo '[Docker 2/2] 验证镜像加速后的 Docker Engine'
  as_root docker info >/dev/null
  echo 'Docker Engine 与 Compose 插件已存在并验证'
  exit 0
fi

test -r /etc/os-release || { echo '无法读取 /etc/os-release' >&2; exit 1; }
. /etc/os-release
case "${ID:-}" in
  debian|ubuntu) distribution=$ID ;;
  *) echo '仅支持在 Debian 或 Ubuntu 上自动安装 Docker' >&2; exit 1 ;;
esac
codename=${VERSION_CODENAME:-}
case "$codename" in
  ''|*[!a-zA-Z0-9.-]*) echo '/etc/os-release 缺少安全的 VERSION_CODENAME' >&2; exit 1 ;;
esac
architecture=$(dpkg --print-architecture)
case "$architecture" in
  ''|*[!a-zA-Z0-9]*) echo '无法识别 dpkg 架构' >&2; exit 1 ;;
esac

STEP=准备下载工具
echo '[Docker 1/6] 准备 ca-certificates 与 curl'
apt_get update
apt_get install -y ca-certificates curl
conflicts=
for package in docker.io docker-compose docker-doc docker-buildx podman-docker containerd runc; do
  if dpkg-query -W -f='${db:Status-Abbrev}' "$package" 2>/dev/null | grep -q '^ii'; then
    conflicts="$conflicts $package"
  fi
done
if [ -n "$conflicts" ]; then
  # shellcheck disable=SC2086
  apt_get remove -y $conflicts
fi

STEP=下载仓库密钥
echo '[Docker 2/6] 下载 Docker 官方仓库密钥'
as_root install -m 0755 -d /etc/apt/keyrings
key_url="https://download.docker.com/linux/$distribution/gpg"
if ! as_root curl --fail --show-error --location \
  --retry 5 --retry-delay 2 --retry-all-errors \
  --connect-timeout 15 --max-time 180 \
  "$key_url" -o /etc/apt/keyrings/docker.asc; then
  echo "无法下载 Docker 仓库密钥：$key_url" >&2
  echo '请检查目标服务器访问 download.docker.com 的网络后重试' >&2
  exit 35
fi
as_root chmod a+r /etc/apt/keyrings/docker.asc
source_text=$(printf '%s\n' \
  'Types: deb' \
  "URIs: https://download.docker.com/linux/$distribution" \
  "Suites: $codename" \
  'Components: stable' \
  "Architectures: $architecture" \
  'Signed-By: /etc/apt/keyrings/docker.asc')
if [ -n "$SUDO" ]; then
  printf '%s\n' "$source_text" | $SUDO tee /etc/apt/sources.list.d/docker.sources >/dev/null
else
  printf '%s\n' "$source_text" > /etc/apt/sources.list.d/docker.sources
fi

STEP=更新Docker仓库
echo '[Docker 3/6] 更新 Docker 软件仓库'
apt_get update
STEP=安装Docker
echo '[Docker 4/6] 安装 Engine、Buildx 与 Compose'
apt_get install -y \
  docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

STEP=启动并验证Docker
echo '[Docker 5/6] 配置 Docker Hub 镜像加速'
configure_registry_mirrors
STEP=启动并验证Docker
echo '[Docker 6/6] 启动并验证 Docker'
as_root systemctl enable --now docker.service containerd.service
if [ "$(id -un)" != root ]; then
  as_root usermod --append --groups docker "$(id -un)"
fi
as_root docker info >/dev/null
docker compose version >/dev/null
echo 'Docker Engine 与 Compose 插件安装完成'
if [ "$(id -un)" != root ]; then
  echo "已将 $(id -un) 加入 docker 组；重新登录后生效"
fi
"""

    def __init__(
        self, host: str, user: str, *, transport: remote.Transport | None = None,
    ) -> None:
        self.host = host
        self.user = user
        self.transport = transport or remote.Transport()

    def install(self) -> None:
        self.transport.ssh_logged(
            remote.target(self.user, self.host),
            ["bash", "-s", "--"], input_text=self.SCRIPT,
            label=f"准备 Docker：{self.user}@{self.host}",
        )


class PythonProvisioner:
    """为源码版 Manager 准备 Python、venv 与项目依赖。"""

    SYSTEM_SCRIPT = r"""set -Eeuo pipefail
if [ "$(id -u)" -eq 0 ]; then
  SUDO=
elif command -v sudo >/dev/null 2>&1 && sudo --non-interactive true; then
  SUDO='sudo --non-interactive'
else
  echo '安装 Python 需要 root 或免密 sudo' >&2
  exit 1
fi
as_root() {
  if [ -n "$SUDO" ]; then $SUDO "$@"; else "$@"; fi
}
echo '[Python 1/3] 安装 Python、pip 与 venv'
as_root env DEBIAN_FRONTEND=noninteractive \
  apt-get -o Dpkg::Use-Pty=0 update
as_root env DEBIAN_FRONTEND=noninteractive \
  apt-get -o Dpkg::Use-Pty=0 install -y python3 python3-pip python3-venv
echo 'Python、pip 与 venv 已就绪'
"""

    VENV_SCRIPT = r"""set -Eeuo pipefail
root=$1
requirements=$2
echo '[Python 2/3] 创建 Manager 虚拟环境'
mkdir -p -- "$root"
python3 -m venv "$root/.venv"
echo '[Python 3/3] 安装 Manager Python 依赖'
"$root/.venv/bin/python" -m pip install --requirement "$requirements"
"$root/.venv/bin/python" -c 'import yaml'
echo 'Manager Python 环境已就绪'
"""

    def __init__(
        self, endpoint: Endpoint, requirements: Path, *,
        transport: remote.Transport | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.requirements = requirements
        self.transport = transport or remote.Transport()

    @property
    def destination(self) -> str:
        return remote.target(self.endpoint.user, self.endpoint.host)

    def install(self) -> None:
        if not self.requirements.is_file():
            raise DeployError(f"缺少 Python 依赖清单：{self.requirements}")
        remote_requirements = self.endpoint.directory + "/.mcsp-requirements.txt"
        self.transport.ssh_logged(
            self.destination, ["bash", "-s", "--"],
            input_text=self.SYSTEM_SCRIPT,
            label=f"准备 Python：{self.destination}",
        )
        self.transport.ssh(
            self.destination, ["mkdir", "-p", self.endpoint.directory],
        )
        try:
            self.transport.scp(
                self.requirements, f"{self.destination}:{remote_requirements}",
            )
            self.transport.ssh_logged(
                self.destination,
                [
                    "bash", "-s", "--", self.endpoint.directory,
                    remote_requirements,
                ],
                input_text=self.VENV_SCRIPT,
                label=f"安装 Manager Python 依赖：{self.destination}",
            )
        finally:
            self.transport.ssh(
                self.destination, ["rm", "-f", remote_requirements],
                check=False,
            )


class RuntimeDeployment:
    """传输运行时，并且只调用运行时公开的管理命令。"""

    def __init__(
        self, source: Path, endpoint: Endpoint, entrypoint: str, *,
        preserve: tuple[str, ...] = (),
        transport: remote.Transport | None = None,
    ) -> None:
        if any(
            not name or name in (".", "..") or "/" in name or "\\" in name
            for name in preserve
        ):
            raise DeployError("保留路径必须是运行时根目录下的单个名称")
        self.source = source
        self.endpoint = endpoint
        self.entrypoint = entrypoint
        self.preserve = ("config.yml", ".env", *preserve)
        self.transport = transport or remote.Transport()

    @property
    def destination(self) -> str:
        return remote.target(self.endpoint.user, self.endpoint.host)

    @property
    def runtime_name(self) -> str:
        return self.entrypoint.removesuffix(".sh").title()

    def _check_source(self) -> None:
        script = self.source / self.entrypoint
        if not script.is_file() or script.stat().st_size == 0:
            raise DeployError(f"缺少运行时入口：{script}")

    def check(self) -> None:
        print(f"[检查 1/3] 检查本地 {self.runtime_name} 入口", flush=True)
        self._check_source()
        script = self.source / self.entrypoint
        print(f"[检查 2/3] 连接 {self.destination}", flush=True)
        self.transport.ssh(self.destination, ["true"], capture=True)
        print(f"[检查 3/3] 验证远端 {self.runtime_name} 运行环境", flush=True)
        self.transport.ssh(
            self.destination, ["bash", "-s", "--", "check"],
            input_text=script.read_text(encoding="utf-8"), capture=True,
        )

    def publish(self) -> None:
        """按文件内容增量发布运行时代码。"""
        print(f"[源码 1/3] 检查本地 {self.runtime_name} 源码", flush=True)
        self._check_source()
        print(
            f"[源码 2/3] 对比 {self.destination} 清单并上传变化文件",
            flush=True,
        )
        uploaded, deleted = self.transport.sync_tree(
            self.source, self.destination, self.endpoint.directory,
            preserve=self.preserve,
        )
        print(f"[源码 3/3] 增量同步完成：上传 {uploaded}，删除 {deleted}")

    def install(self, config: str, environment: str) -> None:
        """同步运行时及配置，然后调用运行时自己的安装逻辑。"""
        self.check()
        self.publish()
        print(f"[部署] 同步 {self.runtime_name} 配置与密钥", flush=True)
        self.upload_config(config)
        self.upload_environment(environment)
        print(f"[部署] 调用 {self.runtime_name} install", flush=True)
        self._invoke("install")

    def configure(self, config: str, environment: str | None = None) -> None:
        """只更新运行时配置；校验及重载行为由运行时自己负责。"""
        print(f"[配置 1/3] 同步 {self.runtime_name} config.yml", flush=True)
        config_changed = self.upload_config(config)
        config_state = "已更新" if config_changed else "无变化"
        print(f"[配置 1/3] config.yml {config_state}", flush=True)
        if environment is not None:
            print(f"[配置 2/3] 同步 {self.runtime_name} .env", flush=True)
            environment_changed = self.upload_environment(environment)
            environment_state = "已更新" if environment_changed else "无变化"
            print(f"[配置 2/3] .env {environment_state}", flush=True)
        else:
            print("[配置 2/3] 无 .env 更新，跳过", flush=True)
        print(f"[配置 3/3] 调用 {self.runtime_name} apply-config", flush=True)
        self._invoke("apply-config")

    def _invoke(self, action: str) -> None:
        command = (
            f"cd {remote.shell_join([self.endpoint.directory])} && "
            f"chmod 700 {remote.shell_join([self.entrypoint])} && "
            f"exec ./{remote.shell_join([self.entrypoint])} "
            f"{remote.shell_join([action])}"
        )
        if action == "install":
            runtime = self.entrypoint.removesuffix(".sh")
            self.transport.ssh_logged(
                self.destination, ["bash", "-lc", command],
                label=f"安装 {runtime} 运行时：{self.destination}",
            )
        else:
            self.transport.ssh_tail(
                self.destination, ["bash", "-lc", command],
                label=f"[{self.runtime_name}] apply-config",
            )

    def upload_config(self, content: str) -> bool:
        return self._upload(content, "config.yml")

    def upload_environment(self, content: str) -> bool:
        return self._upload(content, ".env", mode=0o600)

    def _upload(self, content: str, name: str, *, mode: int = 0o644) -> bool:
        with tempfile.TemporaryDirectory(prefix="mcsp-runtime-") as directory:
            output = Path(directory) / name
            output.write_text(content, encoding="utf-8")
            output.chmod(mode)
            return self.transport.upload_file(
                output, self.destination, self.endpoint.directory + "/" + name,
                mode=mode,
            )
