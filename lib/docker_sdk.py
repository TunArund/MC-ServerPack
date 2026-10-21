"""共享 Docker 命令执行与镜像部署原语。"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any


class DockerError(RuntimeError):
    pass


Runner = Callable[..., subprocess.CompletedProcess[str]]
EventSink = Callable[[str, int, int, str, dict[str, object] | None], None]


def discard_event(
    stage: str, current: int, total: int, message: str,
    detail: dict[str, object] | None = None,
) -> None:
    del stage, current, total, message, detail


class DockerProvisioner:
    """通过已注入的 SSH transport 在远端准备 Docker Engine。"""

    def __init__(self, destination: str, install_script: str, transport: Any) -> None:
        self.destination = destination
        self.install_script = install_script
        self.transport = transport

    def install(self) -> None:
        self.transport.ssh_logged(
            self.destination, ["bash", "-s", "--"],
            input_text=self.install_script,
            label=f"准备 Docker：{self.destination}",
        )


class DockerCommandRunner:
    """只封装 Docker 进程契约，不包含组件生命周期。"""

    def __init__(self, root: Path, runner: Runner = subprocess.run) -> None:
        self.root = root
        self.runner = runner

    def _run(
        self, arguments: Sequence[str], *, capture: bool = False,
        input_text: str | None = None, environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = self.runner(
                list(arguments), cwd=self.root, env=environment, text=True,
                input=input_text, capture_output=capture, check=False,
            )
        except OSError as exc:
            raise DockerError(f"无法执行 {arguments[0]}：{exc}") from exc
        if result.returncode:
            detail = (
                ((result.stderr or result.stdout) if capture else "")
                or "完整输出已记录到服务日志"
            )
            raise DockerError(f"Docker 命令失败：{detail.strip()[-500:]}")
        return result

    def check(self) -> None:
        self._run(["docker", "compose", "version"], capture=True)

    def image_exists(self, image: str) -> bool:
        try:
            result = self.runner(
                ["docker", "image", "inspect", image], cwd=self.root,
                text=True, capture_output=True, check=False,
            )
        except OSError as exc:
            raise DockerError(f"无法执行 docker：{exc}") from exc
        return result.returncode == 0

    def build_image(self, image: str, dockerfile: Path, context: Path | None = None) -> None:
        if not dockerfile.is_file():
            raise DockerError(f"缺少 Dockerfile：{dockerfile}")
        self._run([
            "docker", "build", "-f", str(dockerfile), "-t", image,
            str(context or self.root),
        ])


class DockerClient(DockerCommandRunner):
    """可注入组件配置的 Compose 项目完整操作集合。"""

    def __init__(
        self, root: Path, *, image_resolver: Callable[[int, Path], str],
        compose_renderer: Callable[[Any], Path], runner: Runner = subprocess.run,
        events: EventSink = discard_event, runtime_directory: str = "runtime",
    ) -> None:
        super().__init__(root, runner)
        self.image_resolver = image_resolver
        self.compose_renderer = compose_renderer
        self.events = events
        self.runtime_directory = runtime_directory

    def ensure_image(self, java: int) -> None:
        image = self.image_resolver(java, self.root)
        if self.image_exists(image):
            self.events("image", 1, 1, f"复用已有 Runtime：{image}", {"image": image})
            return
        dockerfile = self.root / self.runtime_directory / f"java{java}.dockerfile"
        if not dockerfile.is_file():
            raise DockerError(f"缺少 Java {java} 镜像定义")
        self.events("image", 1, 1, f"构建 Runtime：{image}", {"image": image})
        self.build_image(image, dockerfile, self.root)

    def _environment(
        self, *, stop_timeout: int | None = None, start_timeout: int | None = None,
    ) -> dict[str, str]:
        uid = os.environ.get("MC_CONTAINER_UID", str(os.getuid()))
        gid = os.environ.get("MC_CONTAINER_GID", str(os.getgid()))
        try:
            uid_number, gid_number = int(uid), int(gid)
        except ValueError as exc:
            raise DockerError("MC_CONTAINER_UID/GID 必须是整数") from exc
        if uid_number == 0:
            raise DockerError("拒绝以 root 运行 Minecraft 容器")
        if uid_number < 0 or gid_number < 0:
            raise DockerError("MC_CONTAINER_UID/GID 不能为负数")
        values = os.environ.copy()
        timeout = str(stop_timeout if stop_timeout is not None else os.environ.get("MC_STOP_TIMEOUT", "180"))
        startup = str(start_timeout if start_timeout is not None else os.environ.get("MC_START_TIMEOUT", "300"))
        values.update({
            "MC_CONTAINER_UID": str(uid_number), "MC_CONTAINER_GID": str(gid_number),
            "MC_STOP_TIMEOUT": timeout, "MC_DOCKER_GRACE": str(int(timeout) + 10),
            "MC_START_TIMEOUT": startup,
        })
        return values

    def compose(
        self, spec: Any, *arguments: str, capture: bool = False,
        input_text: str | None = None, stop_timeout: int | None = None,
        start_timeout: int | None = None,
    ) -> subprocess.CompletedProcess[str]:
        compose_file = self.compose_renderer(spec)
        command = [
            "docker", "compose", "--project-name", f"mc-{spec.pack_id}",
            "--project-directory", str(spec.directory), "-f", str(compose_file), *arguments,
        ]
        return self._run(command, capture=capture, input_text=input_text,
                         environment=self._environment(stop_timeout=stop_timeout,
                                                       start_timeout=start_timeout))

    def running(self, spec: Any) -> bool:
        return bool(self.compose(spec, "ps", "--status", "running", "-q", "minecraft", capture=True).stdout.strip())

    def start(self, spec: Any, *, timeout: int = 300) -> None:
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
                logs = self.compose(spec, "logs", "--tail", "50", "minecraft", capture=True).stdout.strip()
            except DockerError:
                logs = ""
            suffix = f"\nMinecraft 末尾日志：\n{logs[-4000:]}" if logs else ""
            raise DockerError(f"{spec.pack_id} 未通过启动/健康检查：{exc}{suffix}") from exc

    def _wait_healthy(self, spec: Any, timeout: int) -> None:
        started = time.monotonic()
        deadline = started + timeout
        while True:
            container = self.compose(spec, "ps", "-a", "-q", "minecraft", capture=True).stdout.strip()
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
                self.events("start", 4, 4, "容器健康", {"elapsed": int(time.monotonic() - started)})
                return
            if status in ("dead", "exited"):
                raise DockerError(f"容器已退出：status={status}，health={health}，restarts={restarts}")
            elapsed = int(time.monotonic() - started)
            if time.monotonic() >= deadline:
                raise DockerError(f"等待容器健康超时：{elapsed}/{timeout}s，status={status}，health={health}，restarts={restarts}")
            try:
                latest = self.compose(spec, "logs", "--no-color", "--tail", "1", "minecraft", capture=True).stdout.strip().splitlines()
                detail = latest[-1].strip() if latest else "尚无容器日志"
            except DockerError:
                detail = "暂时无法读取容器日志"
            self.events("start", 4, 4, "等待 Minecraft 服务端健康", {
                "elapsed": elapsed, "timeout": timeout, "container_state": status,
                "health": health, "tail": detail[-300:],
            })
            time.sleep(min(2, max(0, deadline - time.monotonic())))

    def stop(self, spec: Any, *, timeout: int = 180) -> None:
        self.check()
        if not self.running(spec):
            self.events("stop", 3, 3, f"{spec.pack_id} 已处于停止状态", None)
            return
        started = time.monotonic()
        self.events("stop", 1, 3, f"向 {spec.pack_id} 发送停止信号", {"timeout": timeout})
        self.compose(spec, "stop", "minecraft", stop_timeout=timeout)
        self.events("stop", 2, 3, "容器已停止，校验退出码", {"elapsed": int(time.monotonic() - started)})
        container = self.compose(spec, "ps", "-a", "-q", "minecraft", capture=True).stdout.strip()
        if not container:
            raise DockerError(f"停止 {spec.pack_id} 后找不到容器")
        result = self._run(["docker", "inspect", "-f", "{{.State.ExitCode}}", container], capture=True)
        if result.stdout.strip() != "0":
            raise DockerError(f"{spec.pack_id} 退出码为 {result.stdout.strip()}，可能未完成存档")
        self.events("stop", 3, 3, f"已确认 {spec.pack_id} 安全退出", {"exit_code": 0})

    def status(self, spec: Any) -> str:
        self.check()
        return self.compose(spec, "ps", capture=True).stdout

    def state(self, spec: Any) -> str:
        container = self.compose(spec, "ps", "-a", "-q", "minecraft", capture=True).stdout.strip()
        if not container:
            return "stopped"
        raw = self._run([
            "docker", "inspect", "-f",
            "{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}",
            container,
        ], capture=True).stdout.strip()
        status, _, health = raw.partition("|")
        if status == "running" and health in ("healthy", "starting", "unhealthy"):
            return health
        valid = ("created", "restarting", "running", "removing", "paused", "exited", "dead")
        return status if status in valid else "unknown"

    def log_text(self, spec: Any, *, lines: int = 100) -> str:
        if lines < 0:
            raise DockerError("日志行数不能为负数")
        return self.compose(spec, "logs", "--no-color", "--tail", str(lines), "minecraft", capture=True).stdout

    def console(self, spec: Any, command: str) -> None:
        self.check()
        if not self.running(spec):
            raise DockerError(f"{spec.pack_id} 未运行")
        self.compose(spec, "exec", "-T", "minecraft", "sh", "-c",
                     "cat > /tmp/mc-manager-console.pipe", input_text=command.rstrip("\n") + "\n")

    def remove_project(self, spec: Any) -> None:
        self.check()
        self.compose(spec, "down", "--remove-orphans")
