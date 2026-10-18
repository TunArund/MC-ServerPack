"""轻量且可注入的 Docker 与 Docker Compose 封装。"""

from __future__ import annotations

import json
import os
import pwd
import re
import shutil
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .config import MANAGER_ROOT, PackSpec, render_compose, runtime_image


class DockerError(RuntimeError):
    pass


Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class DockerInstallResult:
    already_installed: bool
    user_added: str | None


class DockerInstaller:
    """使用 Docker 官方 APT 仓库安装 Engine 与 Compose 插件。"""

    CONFLICTS = (
        "docker.io", "docker-compose", "docker-doc", "docker-buildx",
        "podman-docker", "containerd", "runc",
    )
    PACKAGES = (
        "docker-ce", "docker-ce-cli", "containerd.io",
        "docker-buildx-plugin", "docker-compose-plugin",
    )
    REGISTRY_MIRRORS = (
        "https://docker.1ms.run",
        "https://docker.m.daocloud.io",
        "https://dockerproxy.link",
        "https://free.hubfast.cn",
        "https://proxy.vvvv.ee",
        "https://docker.jiaxin.site",
        "https://docker.xuanyuan.me",
        "https://registry.cyou",
        "https://cf-workers-docker-io-apl.pages.dev",
    )
    CONFIGURE_DAEMON = r"""import json,os,sys,tempfile
path=sys.argv[1]; mirrors=json.loads(sys.argv[2])
try:
    with open(path,encoding='utf-8') as source: config=json.load(source)
except FileNotFoundError: config={}
except (OSError,json.JSONDecodeError) as exc: raise SystemExit(f'无法安全合并 {path}：{exc}')
if not isinstance(config,dict): raise SystemExit(f'{path} 顶层必须是 JSON 对象')
existing=config.get('registry-mirrors',[])
if not isinstance(existing,list) or not all(isinstance(item,str) for item in existing):
    raise SystemExit(f'{path} 的 registry-mirrors 必须是字符串数组')
merged=list(dict.fromkeys([*existing,*mirrors])); changed=merged != existing
config['registry-mirrors']=merged
if 'max-concurrent-downloads' not in config: config['max-concurrent-downloads']=10; changed=True
if 'max-concurrent-uploads' not in config: config['max-concurrent-uploads']=5; changed=True
if changed:
    os.makedirs(os.path.dirname(path),mode=0o755,exist_ok=True)
    descriptor,temporary=tempfile.mkstemp(prefix='daemon.json.mcsp-',dir=os.path.dirname(path))
    try:
        with os.fdopen(descriptor,'w',encoding='utf-8') as output:
            json.dump(config,output,ensure_ascii=False,indent=2); output.write('\n')
        os.chmod(temporary,0o644); os.replace(temporary,path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)
print('changed' if changed else 'unchanged')
"""

    def __init__(
        self, runner: Runner = subprocess.run, *,
        os_release: Path = Path("/etc/os-release"),
        daemon_config: Path = Path("/etc/docker/daemon.json"),
        effective_uid: int | None = None, invoking_user: str | None = None,
    ) -> None:
        self.runner = runner
        self.os_release = os_release
        self.daemon_config = daemon_config
        self.effective_uid = os.geteuid() if effective_uid is None else effective_uid
        self.invoking_user = invoking_user or pwd.getpwuid(os.getuid()).pw_name
        if self.effective_uid == 0:
            self.privilege: list[str] = []
        elif shutil.which("sudo"):
            self.privilege = ["sudo", "--non-interactive"]
        else:
            raise DockerError("安装 Docker 需要 root 或免密 sudo")

    def _call(
        self, arguments: Sequence[str], *, capture: bool = False,
        input_text: str | None = None, check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = self.runner(
                list(arguments), text=True, input=input_text,
                capture_output=capture, check=False,
            )
        except OSError as exc:
            raise DockerError(f"无法执行 {arguments[0]}：{exc}") from exc
        if check and result.returncode:
            detail = ((result.stderr or result.stdout) if capture else "") or "请查看命令输出"
            raise DockerError(
                f"Docker 安装命令失败：{arguments[0]}：{detail.strip()[-500:]}"
            )
        return result

    def _root(self, *arguments: str, **options) -> subprocess.CompletedProcess[str]:
        return self._call([*self.privilege, *arguments], **options)

    def _platform(self) -> tuple[str, str, str]:
        try:
            lines = self.os_release.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise DockerError(f"无法读取 {self.os_release}：{exc}") from exc
        values: dict[str, str] = {}
        for line in lines:
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                values[key] = value.strip().strip("\"'")
        distribution = values.get("ID", "").lower()
        codename = values.get("VERSION_CODENAME", "").lower()
        if distribution not in ("debian", "ubuntu"):
            raise DockerError("仅支持在 Debian 或 Ubuntu 上自动安装 Docker")
        if not re.fullmatch(r"[a-z0-9.-]+", codename):
            raise DockerError("/etc/os-release 缺少安全的 VERSION_CODENAME")
        architecture = self._call(
            ["dpkg", "--print-architecture"], capture=True,
        ).stdout.strip()
        if not re.fullmatch(r"[a-z0-9]+", architecture):
            raise DockerError("无法识别 dpkg 架构")
        return distribution, codename, architecture

    def _installed_conflicts(self) -> list[str]:
        installed = []
        for package in self.CONFLICTS:
            result = self._call(
                ["dpkg-query", "-W", "-f=${db:Status-Abbrev}", package],
                capture=True, check=False,
            )
            if result.returncode == 0 and result.stdout.startswith("ii"):
                installed.append(package)
        return installed

    def _grant_access(self) -> str | None:
        if self.invoking_user == "root":
            return None
        self._root("usermod", "--append", "--groups", "docker", self.invoking_user)
        return self.invoking_user

    def _enable_and_verify(self) -> None:
        self._root("systemctl", "enable", "--now", "docker.service", "containerd.service")
        self._root("docker", "info", capture=True)
        self._call(["docker", "compose", "version"], capture=True)

    def _configure_registry_mirrors(self) -> bool:
        result = self._root(
            "python3", "-c", self.CONFIGURE_DAEMON,
            str(self.daemon_config), json.dumps(self.REGISTRY_MIRRORS),
            capture=True,
        )
        return result.stdout.strip() == "changed"

    def install(self) -> DockerInstallResult:
        docker = self._call(["docker", "--version"], capture=True, check=False)
        compose = self._call(
            ["docker", "compose", "version"], capture=True, check=False,
        )
        if docker.returncode == 0 and compose.returncode == 0:
            user = self._grant_access()
            self._enable_and_verify()
            if self._configure_registry_mirrors():
                self._root("systemctl", "restart", "docker.service")
                self._root("docker", "info", capture=True)
            return DockerInstallResult(True, user)

        distribution, codename, architecture = self._platform()
        self._root("apt-get", "update")
        self._root("apt-get", "install", "-y", "ca-certificates", "curl")
        conflicts = self._installed_conflicts()
        if conflicts:
            self._root("apt-get", "remove", "-y", *conflicts)
        self._root("install", "-m", "0755", "-d", "/etc/apt/keyrings")
        keyring = "/etc/apt/keyrings/docker.asc"
        self._root(
            "curl", "-fsSL",
            f"https://download.docker.com/linux/{distribution}/gpg",
            "-o", keyring,
        )
        self._root("chmod", "a+r", keyring)
        source = (
            "Types: deb\n"
            f"URIs: https://download.docker.com/linux/{distribution}\n"
            f"Suites: {codename}\n"
            "Components: stable\n"
            f"Architectures: {architecture}\n"
            f"Signed-By: {keyring}\n"
        )
        self._root(
            "tee", "/etc/apt/sources.list.d/docker.sources",
            input_text=source, capture=True,
        )
        self._root("apt-get", "update")
        self._root("apt-get", "install", "-y", *self.PACKAGES)
        user = self._grant_access()
        self._configure_registry_mirrors()
        self._enable_and_verify()
        return DockerInstallResult(False, user)


class DockerClient:
    def __init__(self, manager_root: Path = MANAGER_ROOT, runner: Runner = subprocess.run) -> None:
        self.manager_root = manager_root
        self.runner = runner

    def _run(
        self, arguments: Sequence[str], *, capture: bool = False, input_text: str | None = None,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = self.runner(
                list(arguments), cwd=self.manager_root, env=environment, text=True,
                input=input_text, capture_output=capture, check=False,
            )
        except OSError as exc:
            raise DockerError(f"无法执行 {arguments[0]}：{exc}") from exc
        if result.returncode:
            detail = (
                ((result.stderr or result.stdout) if capture else "")
                or "完整输出已记录到 manager.log"
            )
            raise DockerError(f"Docker 命令失败：{detail.strip()[-500:]}")
        return result

    def check(self) -> None:
        self._run(["docker", "compose", "version"], capture=True)

    def ensure_image(self, java: int) -> None:
        image = runtime_image(java, self.manager_root)
        try:
            inspected = self.runner(
                ["docker", "image", "inspect", image], cwd=self.manager_root,
                text=True, capture_output=True, check=False,
            )
        except OSError as exc:
            raise DockerError(f"无法执行 docker：{exc}") from exc
        if inspected.returncode == 0:
            print(f"[镜像] 复用已有 Runtime：{image}", flush=True)
            return
        dockerfile = self.manager_root / "runtime" / f"java{java}.dockerfile"
        if not dockerfile.is_file():
            raise DockerError(f"缺少 Java {java} 镜像定义")
        print(f"[镜像] 未找到 {image}，开始构建", flush=True)
        self._run(["docker", "build", "-f", str(dockerfile), "-t", image, str(self.manager_root)])

    def _environment(
        self, *, stop_timeout: int | None = None, start_timeout: int | None = None,
    ) -> dict[str, str]:
        uid = os.environ.get("MC_CONTAINER_UID", str(os.getuid()))
        gid = os.environ.get("MC_CONTAINER_GID", str(os.getgid()))
        if not uid.isdigit() or not gid.isdigit():
            raise DockerError("MC_CONTAINER_UID/GID 必须是整数")
        if uid == "0":
            raise DockerError("拒绝以 root 运行 Minecraft 容器")
        values = os.environ.copy()
        timeout = str(stop_timeout if stop_timeout is not None else os.environ.get("MC_STOP_TIMEOUT", "180"))
        startup = str(
            start_timeout if start_timeout is not None
            else os.environ.get("MC_START_TIMEOUT", "300")
        )
        values.update({
            "MC_CONTAINER_UID": uid, "MC_CONTAINER_GID": gid,
            "MC_STOP_TIMEOUT": timeout,
            "MC_DOCKER_GRACE": str(int(timeout) + 10),
            "MC_START_TIMEOUT": startup,
        })
        return values

    def compose(
        self, spec: PackSpec, *arguments: str, capture: bool = False,
        input_text: str | None = None, stop_timeout: int | None = None,
        start_timeout: int | None = None,
    ) -> subprocess.CompletedProcess[str]:
        compose_file = render_compose(spec)
        command = [
            "docker", "compose", "--project-name", f"mc-{spec.pack_id}",
            "--project-directory", str(spec.directory), "-f", str(compose_file), *arguments,
        ]
        return self._run(
            command, capture=capture, input_text=input_text,
            environment=self._environment(
                stop_timeout=stop_timeout, start_timeout=start_timeout,
            ),
        )

    def running(self, spec: PackSpec) -> bool:
        result = self.compose(spec, "ps", "--status", "running", "-q", "minecraft", capture=True)
        return bool(result.stdout.strip())

    def start(self, spec: PackSpec, *, timeout: int = 300) -> None:
        self.check()
        if self.running(spec):
            self._wait_healthy(spec, timeout)
            return
        self.ensure_image(spec.java)
        try:
            self.compose(spec, "up", "-d", start_timeout=timeout)
            self._wait_healthy(spec, timeout)
        except DockerError as exc:
            try:
                logs = self.compose(
                    spec, "logs", "--tail", "50", "minecraft", capture=True,
                ).stdout.strip()
            except DockerError:
                logs = ""
            suffix = f"\nMinecraft 末尾日志：\n{logs[-4000:]}" if logs else ""
            raise DockerError(f"{spec.pack_id} 未通过启动/健康检查：{exc}{suffix}") from exc

    def _wait_healthy(self, spec: PackSpec, timeout: int) -> None:
        started = time.monotonic()
        deadline = started + timeout
        while True:
            container = self.compose(
                spec, "ps", "-a", "-q", "minecraft", capture=True,
            ).stdout.strip()
            if not container:
                raise DockerError(f"{spec.pack_id} 启动后找不到容器")
            state = self._run([
                "docker", "inspect", "-f",
                "{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}|{{.RestartCount}}",
                container,
            ], capture=True).stdout.strip().split("|")
            status = state[0] if state else "unknown"
            health = state[1] if len(state) > 1 else "none"
            restarts = state[2] if len(state) > 2 else "?"
            if health == "healthy":
                elapsed = int(time.monotonic() - started)
                print(f"[启动 4/4] 容器健康，耗时 {elapsed}s", flush=True)
                return
            if status in ("dead", "exited"):
                raise DockerError(
                    f"容器已退出：status={status}，health={health}，restarts={restarts}"
                )
            elapsed = int(time.monotonic() - started)
            if time.monotonic() >= deadline:
                raise DockerError(
                    f"等待容器健康超时：{elapsed}/{timeout}s，status={status}，"
                    f"health={health}，restarts={restarts}"
                )
            try:
                latest = self.compose(
                    spec, "logs", "--no-color", "--tail", "1", "minecraft",
                    capture=True,
                ).stdout.strip().splitlines()
                detail = latest[-1].strip() if latest else "尚无容器日志"
            except DockerError:
                detail = "暂时无法读取容器日志"
            print(
                f"[启动 4/4] 已等待 {elapsed}/{timeout}s；{status}/{health}；{detail[-300:]}",
                flush=True,
            )
            time.sleep(min(2, max(0, deadline - time.monotonic())))

    def stop(self, spec: PackSpec, *, timeout: int = 180) -> None:
        self.check()
        if not self.running(spec):
            print(f"[停止 3/3] {spec.pack_id} 已处于停止状态", flush=True)
            return
        started = time.monotonic()
        print(
            f"[停止 1/3] 向 {spec.pack_id} 发送停止信号并等待 Java 退出"
            f"（最长 {timeout}s）",
            flush=True,
        )
        self.compose(spec, "stop", "minecraft", stop_timeout=timeout)
        elapsed = int(time.monotonic() - started)
        print(f"[停止 2/3] 容器已停止，耗时 {elapsed}s；校验退出码", flush=True)
        container = self.compose(spec, "ps", "-a", "-q", "minecraft", capture=True).stdout.strip()
        if not container:
            raise DockerError(f"停止 {spec.pack_id} 后找不到容器")
        result = self._run(["docker", "inspect", "-f", "{{.State.ExitCode}}", container], capture=True)
        if result.stdout.strip() != "0":
            raise DockerError(f"{spec.pack_id} 退出码为 {result.stdout.strip()}，可能未完成存档")
        print(f"[停止 3/3] 已确认 {spec.pack_id} 退出码为 0", flush=True)

    def status(self, spec: PackSpec) -> str:
        self.check()
        return self.compose(spec, "ps", capture=True).stdout

    def logs(self, spec: PackSpec, *, lines: int = 100, follow: bool = False) -> None:
        self.check()
        arguments = ["logs", "--tail", str(lines)]
        if follow:
            arguments.append("--follow")
        arguments.append("minecraft")
        self.compose(spec, *arguments)

    def console(self, spec: PackSpec, command: str) -> None:
        self.check()
        if not self.running(spec):
            raise DockerError(f"{spec.pack_id} 未运行")
        self.compose(
            spec, "exec", "-T", "minecraft", "sh", "-c",
            "cat > /tmp/mc-manager-console.pipe", input_text=command.rstrip("\n") + "\n",
        )

    def remove_project(self, spec: PackSpec) -> None:
        self.check()
        self.compose(spec, "down", "--remove-orphans")
