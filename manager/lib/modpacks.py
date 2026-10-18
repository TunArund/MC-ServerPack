"""Manager 整合包生命周期应用服务。"""

from __future__ import annotations

import re
import shutil
import time
import uuid
from pathlib import Path, PurePosixPath

from .backup import create_backup, resolve_backup, restore_backup
from .config import (
    MANAGER_ROOT,
    PACKS_DIR,
    ManagerLimits,
    PackSpec,
    load_limits,
    load_pack,
    render_compose,
    validate_pack_id,
)
from .docker import DockerClient, DockerError


class ModpackError(RuntimeError):
    pass


class ModpackManager:
    def __init__(
        self, manager_root: Path = MANAGER_ROOT, docker: DockerClient | None = None,
        limits: ManagerLimits | None = None,
    ) -> None:
        self.manager_root = manager_root
        self.packs_dir = manager_root / "modpacks" if manager_root != MANAGER_ROOT else PACKS_DIR
        self.docker = docker or DockerClient(manager_root)
        self._limits = limits

    @property
    def limits(self) -> ManagerLimits:
        if self._limits is None:
            self._limits = load_limits(self.manager_root / "config.yml")
        return self._limits

    def spec(self, pack_id: str) -> PackSpec:
        return load_pack(pack_id, self.packs_dir)

    def specs(self) -> list[PackSpec]:
        result = []
        for config_file in sorted(self.packs_dir.glob("*/config.yml")):
            if config_file.parent.name != "template":
                result.append(self.spec(config_file.parent.name))
        return result

    def list_packs(self) -> list[tuple[PackSpec, bool | None]]:
        result: list[tuple[PackSpec, bool | None]] = []
        for spec in self.specs():
            try:
                running: bool | None = self.docker.running(spec)
            except DockerError:
                running = None
            result.append((spec, running))
        return result

    def create(
        self, pack_id: str, *, force: bool = False,
    ) -> Path:
        validate_pack_id(pack_id)
        target = self.packs_dir / pack_id
        exists = target.exists() or target.is_symlink()
        if exists and not force:
            raise ModpackError(f"整合包已存在：{pack_id}")
        template = self.packs_dir / "template"
        if not template.is_dir():
            raise ModpackError("缺少 modpacks/template")
        temporary_root = self.manager_root / "tmp"
        temporary_root.mkdir(parents=True, exist_ok=True)
        staging = temporary_root / f"{pack_id}.create-{uuid.uuid4().hex}"
        backup: Path | None = None
        try:
            shutil.copytree(
                template, staging,
                ignore=shutil.ignore_patterns("compose.yml", "__pycache__"),
            )
            config_file = staging / "config.yml"
            content = config_file.read_text(encoding="utf-8")
            content = re.sub(r"(?m)^name:\s*.*$", f"name: {pack_id}", content, count=1)
            config_file.write_text(content, encoding="utf-8")
            for name in ("src", "data", "backups"):
                (staging / name).mkdir(exist_ok=True)
            if exists:
                backup = temporary_root / f"{pack_id}.replace-{uuid.uuid4().hex}"
                target.replace(backup)
            try:
                staging.replace(target)
            except Exception:
                if backup is not None:
                    backup.replace(target)
                raise
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        if backup is not None:
            if backup.is_dir() and not backup.is_symlink():
                shutil.rmtree(backup, ignore_errors=True)
            else:
                backup.unlink(missing_ok=True)
        return target

    def configure(self, pack_id: str) -> Path:
        spec = self.spec(pack_id)
        self._validate_launch(spec)
        return render_compose(spec)

    def _running(self) -> list[PackSpec]:
        return [spec for spec in self.specs() if self.docker.running(spec)]

    def broadcast_all(self, minutes: int) -> tuple[list[str], list[tuple[str, str]]]:
        sent: list[str] = []
        failed: list[tuple[str, str]] = []
        for spec in self.specs():
            try:
                if self.docker.running(spec):
                    self.docker.console(spec, f"say 服务器将在 {minutes} 分钟后关闭")
                    sent.append(spec.pack_id)
            except Exception as exc:
                failed.append((spec.pack_id, str(exc)))
        return sent, failed

    def _check_capacity(self, wanted: PackSpec, active: list[PackSpec]) -> None:
        if any(spec.port == wanted.port for spec in active):
            owner = next(spec.pack_id for spec in active if spec.port == wanted.port)
            raise ModpackError(f"端口 {wanted.port} 已被 {owner} 使用")
        if len(active) >= self.limits.max_running:
            raise ModpackError(f"已达最大运行数 {self.limits.max_running}；请先显式停止其他整合包")
        total = sum(spec.memory_bytes for spec in active) + wanted.memory_bytes
        if total > self.limits.memory_bytes:
            raise ModpackError(f"启动后内存上限将超过 {self.limits.memory}")

    @staticmethod
    def _source_file(spec: PackSpec, value: str, field: str) -> Path:
        relative = PurePosixPath(value)
        if relative.is_absolute() or any(part in ("", ".", "..") for part in relative.parts):
            raise ModpackError(f"{field} 必须引用 src/ 内的安全相对路径：{value}")
        target = spec.directory / "src" / relative.as_posix()
        if not target.is_file() or target.is_symlink():
            raise ModpackError(f"缺少启动文件：src/{relative.as_posix()}")
        return target

    def _validate_launch(self, spec: PackSpec) -> None:
        command = spec.command
        if command == ("mc-auto",):
            arguments = [
                path for path in (spec.directory / "src").rglob("unix_args.txt")
                if path.is_file() and not path.is_symlink()
            ]
            jars = [
                path for path in (spec.directory / "src").glob("*.jar")
                if path.is_file() and not path.is_symlink()
            ]
            if len(arguments) > 1:
                raise ModpackError("检测到多个 unix_args.txt，无法自动选择启动入口")
            if not arguments and len(jars) != 1:
                raise ModpackError(
                    f"自动启动要求 src/ 中恰有一个顶层 JAR；当前找到 {len(jars)} 个"
                )
        elif command[0] == "java" and "-jar" in command:
            index = command.index("-jar") + 1
            if index >= len(command):
                raise ModpackError("command 中 -jar 后必须指定服务端文件")
            self._source_file(spec, command[index], "command")
        elif command[0] in ("bash", "sh") and len(command) > 1:
            self._source_file(spec, command[1].removeprefix("./"), "command")

    @staticmethod
    def _runtime_file(spec: PackSpec, name: str) -> Path:
        persistent = any(item.path == name for item in spec.persistent)
        return spec.directory / ("data" if persistent else "src") / name

    @classmethod
    def _accept_eula(cls, spec: PackSpec) -> None:
        if not spec.eula:
            raise ModpackError(
                "未接受 Minecraft EULA；请在整合包 config.yml 中设置 eula: true"
            )
        target = cls._runtime_file(spec, "eula.txt")
        if target.is_symlink():
            raise ModpackError(f"EULA 文件不能是符号链接：{target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("eula=true\n", encoding="utf-8")

    @classmethod
    def _set_server_port(cls, spec: PackSpec) -> None:
        cls._write_server_property(spec, "server-port", str(spec.port))

    @classmethod
    def _write_server_property(cls, spec: PackSpec, key: str, value: str) -> Path:
        target = cls._runtime_file(spec, "server.properties")
        if target.is_symlink():
            raise ModpackError(f"服务端配置不能是符号链接：{target}")
        try:
            lines = target.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            lines = []
        updated: list[str] = []
        replaced = False
        for line in lines:
            if re.match(rf"^\s*{re.escape(key)}\s*[=:]", line):
                if not replaced:
                    updated.append(f"{key}={value}")
                    replaced = True
                continue
            updated.append(line)
        if not replaced:
            updated.append(f"{key}={value}")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.tmp-{uuid.uuid4().hex}")
        try:
            temporary.write_text("\n".join(updated) + "\n", encoding="utf-8")
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        return target

    @staticmethod
    def _validate_property(key: str, value: str | None = None) -> None:
        if not re.fullmatch(r"[A-Za-z0-9._-]+", key):
            raise ModpackError("服务端属性名仅允许字母、数字、点、下划线和减号")
        if value is not None and ("\n" in value or "\r" in value):
            raise ModpackError("服务端属性值不能包含换行")

    def get_property(self, pack_id: str, key: str) -> tuple[str, Path]:
        self._validate_property(key)
        spec = self.spec(pack_id)
        target = self._runtime_file(spec, "server.properties")
        if target.is_symlink():
            raise ModpackError(f"服务端配置不能是符号链接：{target}")
        try:
            lines = target.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError as exc:
            raise ModpackError(f"服务端配置尚未生成：{target}") from exc
        pattern = re.compile(rf"^\s*{re.escape(key)}\s*[=:](.*)$")
        for line in lines:
            match = pattern.match(line)
            if match:
                return match.group(1).strip(), target
        raise ModpackError(f"服务端属性不存在：{key}")

    def set_property(self, pack_id: str, key: str, value: str) -> tuple[Path, bool]:
        self._validate_property(key, value)
        spec = self.spec(pack_id)
        target = self._write_server_property(spec, key, value)
        return target, self.docker.running(spec)

    def start(self, pack_id: str) -> None:
        print(f"[启动 1/4] 读取并验证整合包配置：{pack_id}", flush=True)
        wanted = self.spec(pack_id)
        self._validate_launch(wanted)
        self._set_server_port(wanted)
        print("[启动 2/4] 检查运行状态、端口与内存限额", flush=True)
        if self.docker.running(wanted):
            print("[启动 4/4] 容器进程已运行，等待健康检查", flush=True)
            self.docker.start(wanted, timeout=self.limits.start_timeout)
            return
        print("[启动 3/4] 准备 EULA 与运行目录", flush=True)
        self._accept_eula(wanted)
        self._check_capacity(wanted, self._running())
        print("[启动 4/4] 准备 Java 镜像并启动容器", flush=True)
        self.docker.start(wanted, timeout=self.limits.start_timeout)

    def stop(self, pack_id: str) -> None:
        self.docker.stop(self.spec(pack_id), timeout=self.limits.stop_timeout)

    def status(self, pack_id: str) -> str:
        return self.docker.status(self.spec(pack_id))

    def logs(self, pack_id: str, *, lines: int = 100, follow: bool = False) -> None:
        self.docker.logs(self.spec(pack_id), lines=lines, follow=follow)

    def console(self, pack_id: str, command: str) -> None:
        if not command.strip():
            raise ModpackError("控制台命令不能为空")
        self.docker.console(self.spec(pack_id), command)

    def backup(self, pack_id: str) -> Path:
        spec = self.spec(pack_id)
        running = self.docker.running(spec)
        if running:
            self.docker.console(spec, "save-off\nsave-all flush")
            time.sleep(spec.backup_quiesce_seconds)
        try:
            return create_backup(spec)
        finally:
            if running:
                self.docker.console(spec, "save-on")

    def restore(self, pack_id: str, name: str | None = None) -> Path:
        spec = self.spec(pack_id)
        if self.docker.running(spec):
            raise ModpackError("恢复前必须先停止整合包")
        source = resolve_backup(spec, name)
        if (spec.directory / "data").exists():
            create_backup(spec, prune=False)
        restored = restore_backup(spec, source.name)
        return restored

    def remove(self, pack_id: str, *, force: bool = False) -> None:
        spec = self.spec(pack_id)
        if self.docker.running(spec):
            raise ModpackError("删除前必须先停止整合包")
        protected = []
        for directory_name in ("data", "backups"):
            directory = spec.directory / directory_name
            if directory.is_dir() and any(path.is_file() and path.name != ".gitkeep" for path in directory.rglob("*")):
                protected.append(directory_name)
        if protected and not force:
            raise ModpackError(f"整合包含有 {'/'.join(protected)} 数据；确认后使用 --force")
        self.docker.remove_project(spec)
        shutil.rmtree(spec.directory)

    def stop_all(self) -> tuple[list[str], list[tuple[str, str]]]:
        stopped: list[str] = []
        failed: list[tuple[str, str]] = []
        for spec in self.specs():
            try:
                if self.docker.running(spec):
                    self.docker.stop(spec, timeout=self.limits.stop_timeout)
                    stopped.append(spec.pack_id)
            except Exception as exc:  # Packs are independent; collect failures after trying all.
                failed.append((spec.pack_id, str(exc)))
        return stopped, failed
