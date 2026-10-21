"""Manager 整合包生命周期应用服务。"""

from __future__ import annotations

import re
import json
import shutil
import time
import uuid
from datetime import datetime
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo

from .backup import (
    create_backup, finish_restore, list_backups, resolve_backup,
    restore_backup, rollback_restore,
)
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
from .docker import DockerClient, DockerError, EventSink, _discard_event
from .modpack_sdk import ModpackError, ModpackWorkspace


class ModpackManager:
    def __init__(
        self, manager_root: Path = MANAGER_ROOT, docker: DockerClient | None = None,
        limits: ManagerLimits | None = None, events: EventSink = _discard_event,
    ) -> None:
        self.manager_root = manager_root
        self.packs_dir = manager_root / "modpacks" if manager_root != MANAGER_ROOT else PACKS_DIR
        self.events = events
        self.docker = docker or DockerClient(manager_root, events=events)
        self._limits = limits

    @property
    def limits(self) -> ManagerLimits:
        if self._limits is None:
            self._limits = load_limits(self.manager_root / "data/config.yml")
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

    @property
    def state_path(self) -> Path:
        return self.manager_root / "data/state.json"

    def _state(self) -> dict:
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            value = {"format": 1, "modpacks": {}}
        except (OSError, json.JSONDecodeError) as exc:
            raise ModpackError(f"Manager 状态读取失败：{exc}") from exc
        if not isinstance(value, dict) or not isinstance(value.get("modpacks"), dict):
            raise ModpackError("Manager 状态格式无效")
        return value

    def _write_state(self, value: dict) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.state_path)

    def enabled(self, pack_id: str) -> bool:
        validate_pack_id(pack_id)
        item = self._state()["modpacks"].get(pack_id, {})
        return isinstance(item, dict) and item.get("enabled") is True

    def maintenance(self, pack_id: str) -> bool:
        validate_pack_id(pack_id)
        item = self._state()["modpacks"].get(pack_id, {})
        return isinstance(item, dict) and item.get("maintenance") is True

    def set_maintenance(self, pack_id: str, enabled: bool) -> None:
        state = self._state(); item = state["modpacks"].setdefault(pack_id, {})
        if enabled: item["maintenance"] = True
        else: item.pop("maintenance", None)
        self._write_state(state)

    def forget(self, pack_id: str) -> None:
        validate_pack_id(pack_id)
        state = self._state(); state["modpacks"].pop(pack_id, None); self._write_state(state)

    def set_enabled(self, pack_id: str, enabled: bool, *, now: bool = False) -> None:
        self.spec(pack_id)
        state = self._state()
        state["modpacks"].setdefault(pack_id, {})["enabled"] = enabled
        self._write_state(state)
        if now:
            (self.start if enabled else self.stop)(pack_id)

    def details(self, pack_id: str) -> dict[str, object]:
        spec = self.spec(pack_id)
        return {
            "id": spec.pack_id, "name": spec.name, "version": spec.version,
            "java": spec.java, "memory": spec.memory, "port": spec.port,
            "state": "maintenance" if self.maintenance(pack_id) else self.docker.state(spec),
            "enabled": self.enabled(pack_id),
        }

    def summaries(self) -> list[dict[str, object]]:
        return [self.details(spec.pack_id) for spec in self.specs()]

    def create(
        self, pack_id: str, *, force: bool = False,
    ) -> Path:
        validate_pack_id(pack_id)
        target = self.packs_dir / pack_id
        exists = target.exists() or target.is_symlink()
        if exists and not force:
            raise ModpackError(f"整合包已存在：{pack_id}")
        if exists:
            try:
                running = self.docker.running(self.spec(pack_id))
            except Exception:
                running = False
            if running:
                raise ModpackError("整合包正在运行，不能强制替换；请先停止")
        target = ModpackWorkspace(self.packs_dir).create(pack_id, force=force)
        for name in ("data", "backups"):
            (target / name).mkdir(exist_ok=True)
        self.forget(pack_id)
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
        self.events("start", 1, 4, f"读取并验证整合包配置：{pack_id}", None)
        wanted = self.spec(pack_id)
        self._validate_launch(wanted)
        self._set_server_port(wanted)
        self.events("start", 2, 4, "检查运行状态、端口与内存限额", None)
        if self.docker.running(wanted):
            self.events("start", 4, 4, "容器进程已运行，等待健康检查", None)
            self.docker.start(wanted, timeout=self.limits.start_timeout)
            return
        self.events("start", 3, 4, "准备 EULA 与运行目录", None)
        self._accept_eula(wanted)
        self._check_capacity(wanted, self._running())
        self.events("start", 4, 4, "准备 Java 镜像并启动容器", None)
        self.docker.start(wanted, timeout=self.limits.start_timeout)

    def stop(self, pack_id: str) -> None:
        self.docker.stop(self.spec(pack_id), timeout=self.limits.stop_timeout)

    def status(self, pack_id: str) -> str:
        return self.docker.status(self.spec(pack_id))

    def log_text(self, pack_id: str, *, lines: int = 100) -> str:
        return self.docker.log_text(self.spec(pack_id), lines=lines)

    def remove_container(self, pack_id: str) -> None:
        spec = self.spec(pack_id)
        if self.docker.running(spec):
            raise ModpackError("删除容器前必须先停止整合包")
        self.docker.remove_project(spec)
        (spec.directory / "compose.yml").unlink(missing_ok=True)

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
            return create_backup(spec, datetime.now(ZoneInfo(self.limits.timezone)))
        finally:
            if running:
                self.docker.console(spec, "save-on")

    def backups(self, pack_id: str) -> list[Path]:
        return list_backups(self.spec(pack_id))

    def restore(
        self, pack_id: str, name: str | None = None, *, auto_restart: bool = False,
    ) -> Path:
        spec = self.spec(pack_id)
        was_running = self.docker.running(spec)
        if was_running and not auto_restart:
            raise ModpackError("恢复前必须先停止整合包，或使用 --auto-restart")
        # Resolve the selector before changing runtime state. A typo or an
        # ambiguous short ID must never stop a healthy server.
        source = resolve_backup(spec, name)
        if was_running:
            self.docker.stop(spec, timeout=self.limits.stop_timeout)
        if (spec.directory / "data").exists():
            create_backup(
                spec, datetime.now(ZoneInfo(self.limits.timezone)), prune=False,
            )
        restored = restore_backup(spec, source.name, keep_previous=True)
        try:
            if was_running:
                self.start(pack_id)
        except Exception:
            rollback_restore(spec)
            if was_running:
                try: self.start(pack_id)
                except Exception: pass
            raise
        finish_restore(spec)
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
        self.forget(pack_id)

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
