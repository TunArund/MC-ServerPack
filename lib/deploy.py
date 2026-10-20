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


class RuntimeDeployment:
    """传输运行时，并且只调用运行时公开的管理命令。"""

    def __init__(
        self, source: Path, endpoint: Endpoint, entrypoint: str, *,
        preserve: tuple[str, ...] = (),
        config_name: str = "config.yml", environment_name: str = ".env",
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
        self.config_name = config_name
        self.environment_name = environment_name
        self.preserve = tuple(dict.fromkeys((config_name.split("/", 1)[0], environment_name.split("/", 1)[0], *preserve)))
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

    def install(
        self, config: str, environment: str, *,
        recreate_on_config: bool = False,
        files: tuple[tuple[Path, str, int], ...] = (),
    ) -> None:
        """同步运行时及配置，然后调用运行时自己的安装逻辑。"""
        self.check()
        self.publish()
        print(f"[部署] 同步 {self.runtime_name} 配置与密钥", flush=True)
        config_changed = self.upload_config(config)
        environment_changed = self.upload_environment(environment)
        self._upload_files(files)
        arguments: tuple[str, ...] = ()
        if recreate_on_config and (config_changed or environment_changed):
            arguments = ("--recreate",)
        print(f"[部署] 调用 {self.runtime_name} install", flush=True)
        self._invoke("install", arguments)

    def configure(
        self, config: str, environment: str | None = None, *,
        files: tuple[tuple[Path, str, int], ...] = (),
    ) -> None:
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
        self._upload_files(files)
        print(f"[配置 3/3] 调用 {self.runtime_name} apply-config", flush=True)
        self._invoke("apply-config")

    def _upload_files(self, files: tuple[tuple[Path, str, int], ...]) -> None:
        for source, name, mode in files:
            print(f"[部署] 上传 {name}", flush=True)
            self.upload_path(source, name, mode)

    def upload_path(self, source: Path, name: str, mode: int = 0o600) -> bool:
        source = Path(source)
        if not source.is_file():
            raise DeployError(f"缺少待上传文件：{source}")
        return self.transport.upload_file(
            source, self.destination,
            self.endpoint.directory + "/" + name, mode=mode,
        )

    def download_path(
        self, name: str, local_path: Path, *, mode: int = 0o600,
        optional: bool = False,
    ) -> bool:
        remote_path = self.endpoint.directory + "/" + name
        if optional and not self.transport.remote_file_exists(
            self.destination, remote_path,
        ):
            return False
        self.transport.download_file(
            self.destination, remote_path, local_path, mode=mode,
        )
        return True

    def _invoke(self, action: str, arguments: tuple[str, ...] = ()) -> None:
        tail = remote.shell_join([action])
        if arguments:
            tail += " " + " ".join(remote.shell_join([arg]) for arg in arguments)
        command = (
            f"cd {remote.shell_join([self.endpoint.directory])} && "
            f"chmod 700 {remote.shell_join([self.entrypoint])} && "
            f"exec ./{remote.shell_join([self.entrypoint])} {tail}"
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
        return self._upload(content, self.config_name)

    def upload_environment(self, content: str) -> bool:
        return self._upload(content, self.environment_name, mode=0o600)

    def _upload(self, content: str, name: str, *, mode: int = 0o644) -> bool:
        with tempfile.TemporaryDirectory(prefix="mcsp-runtime-") as directory:
            output = Path(directory) / Path(name).name
            output.write_text(content, encoding="utf-8")
            output.chmod(mode)
            return self.transport.upload_file(
                output, self.destination, self.endpoint.directory + "/" + name,
                mode=mode,
            )
