"""轻量且可注入的 Docker 与 Docker Compose 封装。"""

from __future__ import annotations

import os
import pwd
import re
import shutil
import subprocess
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

    def __init__(
        self, runner: Runner = subprocess.run, *,
        os_release: Path = Path("/etc/os-release"),
        effective_uid: int | None = None, invoking_user: str | None = None,
    ) -> None:
        self.runner = runner
        self.os_release = os_release
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

    def install(self) -> DockerInstallResult:
        docker = self._call(["docker", "--version"], capture=True, check=False)
        compose = self._call(
            ["docker", "compose", "version"], capture=True, check=False,
        )
        if docker.returncode == 0 and compose.returncode == 0:
            user = self._grant_access()
            self._enable_and_verify()
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
            detail = ((result.stderr or result.stdout) if capture else "") or "请查看 Docker 输出"
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
            return
        dockerfile = self.manager_root / "runtime" / f"java{java}.dockerfile"
        if not dockerfile.is_file():
            raise DockerError(f"缺少 Java {java} 镜像定义")
        self._run(["docker", "build", "-f", str(dockerfile), "-t", image, str(self.manager_root)])

    def _environment(self, *, stop_timeout: int | None = None) -> dict[str, str]:
        uid = os.environ.get("MC_CONTAINER_UID", str(os.getuid()))
        gid = os.environ.get("MC_CONTAINER_GID", str(os.getgid()))
        if not uid.isdigit() or not gid.isdigit():
            raise DockerError("MC_CONTAINER_UID/GID 必须是整数")
        if uid == "0":
            raise DockerError("拒绝以 root 运行 Minecraft 容器")
        values = os.environ.copy()
        timeout = str(stop_timeout if stop_timeout is not None else os.environ.get("MC_STOP_TIMEOUT", "180"))
        values.update({
            "MC_CONTAINER_UID": uid, "MC_CONTAINER_GID": gid,
            "MC_STOP_TIMEOUT": timeout,
            "MC_DOCKER_GRACE": str(int(timeout) + 10),
        })
        return values

    def compose(
        self, spec: PackSpec, *arguments: str, capture: bool = False,
        input_text: str | None = None, stop_timeout: int | None = None,
    ) -> subprocess.CompletedProcess[str]:
        compose_file = render_compose(spec)
        command = [
            "docker", "compose", "--project-name", f"mc-{spec.pack_id}",
            "--project-directory", str(spec.directory), "-f", str(compose_file), *arguments,
        ]
        return self._run(
            command, capture=capture, input_text=input_text,
            environment=self._environment(stop_timeout=stop_timeout),
        )

    def running(self, spec: PackSpec) -> bool:
        result = self.compose(spec, "ps", "--status", "running", "-q", "minecraft", capture=True)
        return bool(result.stdout.strip())

    def start(self, spec: PackSpec, *, timeout: int = 300) -> None:
        self.check()
        if self.running(spec):
            return
        self.ensure_image(spec.java)
        self.compose(spec, "up", "-d", "--wait", "--wait-timeout", str(timeout))

    def stop(self, spec: PackSpec, *, timeout: int = 180) -> None:
        self.check()
        if not self.running(spec):
            return
        self.compose(spec, "stop", "minecraft", stop_timeout=timeout)
        container = self.compose(spec, "ps", "-a", "-q", "minecraft", capture=True).stdout.strip()
        if not container:
            raise DockerError(f"停止 {spec.pack_id} 后找不到容器")
        result = self._run(["docker", "inspect", "-f", "{{.State.ExitCode}}", container], capture=True)
        if result.stdout.strip() != "0":
            raise DockerError(f"{spec.pack_id} 退出码为 {result.stdout.strip()}，可能未完成存档")

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
