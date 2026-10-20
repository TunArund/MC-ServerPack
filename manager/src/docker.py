"""Manager 专用的 Minecraft 镜像与 Docker Compose 封装。"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from .config import MANAGER_ROOT, PackSpec, render_compose, runtime_image


class DockerError(RuntimeError):
    pass


Runner = Callable[..., subprocess.CompletedProcess[str]]
EventSink = Callable[[str, int, int, str, dict[str, object] | None], None]


def _discard_event(
    stage: str, current: int, total: int, message: str,
    detail: dict[str, object] | None = None,
) -> None:
    del stage, current, total, message, detail


class DockerClient:
    def __init__(
        self, manager_root: Path = MANAGER_ROOT, runner: Runner = subprocess.run,
        events: EventSink = _discard_event,
    ) -> None:
        self.manager_root = manager_root
        self.runner = runner
        self.events = events

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
            self.events("image", 1, 1, f"复用已有 Runtime：{image}", {"image": image})
            return
        dockerfile = self.manager_root / "runtime" / f"java{java}.dockerfile"
        if not dockerfile.is_file():
            raise DockerError(f"缺少 Java {java} 镜像定义")
        self.events("image", 1, 1, f"构建 Runtime：{image}", {"image": image})
        self._run(["docker", "build", "-f", str(dockerfile), "-t", image, str(self.manager_root)])

    def _environment(
        self, *, stop_timeout: int | None = None, start_timeout: int | None = None,
    ) -> dict[str, str]:
        uid = os.environ.get("MC_CONTAINER_UID", str(os.getuid()))
        gid = os.environ.get("MC_CONTAINER_GID", str(os.getgid()))
        try:
            uid_number = int(uid)
            gid_number = int(gid)
        except ValueError as exc:
            raise DockerError("MC_CONTAINER_UID/GID 必须是整数") from exc
        if uid_number == 0:
            raise DockerError("拒绝以 root 运行 Minecraft 容器")
        if uid_number < 0 or gid_number < 0:
            raise DockerError("MC_CONTAINER_UID/GID 不能为负数")
        uid = str(uid_number)
        gid = str(gid_number)
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
                self.events("start", 4, 4, "容器健康", {"elapsed": elapsed})
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
            self.events("start", 4, 4, "等待 Minecraft 服务端健康", {
                "elapsed": elapsed, "timeout": timeout, "container_state": status,
                "health": health, "tail": detail[-300:],
            })
            time.sleep(min(2, max(0, deadline - time.monotonic())))

    def stop(self, spec: PackSpec, *, timeout: int = 180) -> None:
        self.check()
        if not self.running(spec):
            self.events("stop", 3, 3, f"{spec.pack_id} 已处于停止状态", None)
            return
        started = time.monotonic()
        self.events("stop", 1, 3, f"向 {spec.pack_id} 发送停止信号", {"timeout": timeout})
        self.compose(spec, "stop", "minecraft", stop_timeout=timeout)
        elapsed = int(time.monotonic() - started)
        self.events("stop", 2, 3, "容器已停止，校验退出码", {"elapsed": elapsed})
        container = self.compose(spec, "ps", "-a", "-q", "minecraft", capture=True).stdout.strip()
        if not container:
            raise DockerError(f"停止 {spec.pack_id} 后找不到容器")
        result = self._run(["docker", "inspect", "-f", "{{.State.ExitCode}}", container], capture=True)
        if result.stdout.strip() != "0":
            raise DockerError(f"{spec.pack_id} 退出码为 {result.stdout.strip()}，可能未完成存档")
        self.events("stop", 3, 3, f"已确认 {spec.pack_id} 安全退出", {"exit_code": 0})

    def status(self, spec: PackSpec) -> str:
        self.check()
        return self.compose(spec, "ps", capture=True).stdout

    def state(self, spec: PackSpec) -> str:
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
            return {"healthy": "healthy", "starting": "starting", "unhealthy": "unhealthy"}[health]
        return status if status in ("created", "restarting", "running", "removing", "paused", "exited", "dead") else "unknown"

    def log_text(self, spec: PackSpec, *, lines: int = 100) -> str:
        if lines < 0:
            raise DockerError("日志行数不能为负数")
        return self.compose(
            spec, "logs", "--no-color", "--tail", str(lines), "minecraft", capture=True,
        ).stdout

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
