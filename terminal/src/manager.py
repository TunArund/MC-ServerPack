"""Terminal 对 Manager 公共命令接口的薄封装。"""

from __future__ import annotations

import json
import os
import re
import shutil
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

import yaml

from . import remote
from .config import (
    CONFIG_FILE, ROOT, SOURCE_ROOT, ManagerConfig, load_env, load_manager,
    render_runtime_section,
)
from .deploy import RuntimeDeployment
from .output import renderer
from .manager_sdk import ManagerSdk, ManagerSdkError, format_job_event
from .power import Power, PowerError, PowerTarget, create_power


class ManagerError(ManagerSdkError, remote.CommandError):
    """Manager 未遵循约定的公共命令协议。"""


class _JobProgress:
    def __init__(self) -> None:
        self.last_stage = None
        self.rendered = False

    def __call__(self, event: dict) -> None:
        line = format_job_event(event)
        signature = (event.get("stage"), event.get("current"), event.get("total"), line)
        if not line or signature == self.last_stage:
            return
        self.last_stage = signature
        if renderer.progress_is_tty:
            renderer.progress(
                "\r\033[2K" + line[:max(20, shutil.get_terminal_size().columns - 1)],
                end="",
            )
            self.rendered = True
        else:
            renderer.progress(line)

    def finish(self) -> None:
        if self.rendered:
            renderer.progress("")


class Manager:
    """调用 Manager 公共入口，不实现整合包和容器业务。"""

    def __init__(
        self, config_path: Path = CONFIG_FILE, *, config: Optional[ManagerConfig] = None,
        transport: Optional[remote.Transport] = None,
    ) -> None:
        self.config_path = config_path
        self.config = config or load_manager(config_path)
        self.transport = transport or remote.Transport()
        self.tls_directory = SOURCE_ROOT / "manager/data/tls"
        self.endpoint = self.config.endpoint
        self.deployment = RuntimeDeployment(
            SOURCE_ROOT / "manager", self.endpoint, "manager.sh",
            preserve=(
                "modpacks", "data",
            ),
            config_name="data/config.yml", environment_name="data/.env",
            transport=self.transport,
        )

    def _sdk(self) -> ManagerSdk:
        values = load_env(self.config_path.parent / ".env")
        secret = values.get("MANAGER_API_KEY", "")
        if not secret:
            raise ManagerError(
                ".env 缺少 MANAGER_API_KEY；请先执行 ./terminal/mcsp.sh deploy manager"
            )
        ca = self.tls_directory / "ca.crt"
        return ManagerSdk(
            self.config.api_host, self.config.api_port, secret,
            identity="admin", ca=ca if ca.is_file() else None,
        )

    def _api_request(
        self, method: str, path: str, body: dict | bytes | None = None,
    ) -> dict:
        return self._sdk().request(method, path, body)

    def _fetch_bootstrap(self) -> None:
        """部署完成后取回 Manager 生成的 API 密钥和 CA，供本机及 Monitor 复用。"""
        with tempfile.TemporaryDirectory(prefix="mcsp-manager-") as directory:
            env_path = Path(directory) / "env"
            self.deployment.download_path("data/.env", env_path)
            values = load_env(env_path)
        key = values.get("MANAGER_API_KEY", "").strip()
        if len(key) < 32:
            raise ManagerError("Manager 未生成有效的 MANAGER_API_KEY")
        self._store_api_key(key)
        ca = self.tls_directory / "ca.crt"
        if not self.deployment.download_path(
            "data/tls/ca.crt", ca, mode=0o644, optional=True,
        ):
            ca.unlink(missing_ok=True)

    def _store_api_key(self, key: str) -> None:
        env_path = self.config_path.parent / ".env"
        try:
            lines = env_path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            lines = []
        except OSError as exc:
            raise ManagerError(f"无法读取 {env_path}：{exc}") from exc
        output: list[str] = []
        replaced = False
        for line in lines:
            if line.startswith("MANAGER_API_KEY="):
                output.append(f"MANAGER_API_KEY={key}")
                replaced = True
            else:
                output.append(line)
        if not replaced:
            output.append(f"MANAGER_API_KEY={key}")
        temporary = env_path.with_name(env_path.name + ".tmp")
        try:
            temporary.write_text("\n".join(output) + "\n", encoding="utf-8")
            temporary.chmod(0o600)
            os.replace(temporary, env_path)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise ManagerError(f"无法写入 {env_path}：{exc}") from exc

    @property
    def destination(self) -> str:
        return remote.target(self.endpoint.user, self.endpoint.host)

    def check(self) -> None:
        self.deployment.check()

    def install(self) -> None:
        self.deployment.install(
            render_runtime_section(self.config_path, "manager"),
            self._environment(),
        )
        self._fetch_bootstrap()

    def publish(self) -> None:
        self.deployment.publish()

    def configure(self) -> None:
        self.deployment.configure(
            render_runtime_section(self.config_path, "manager"),
            self._environment(),
        )
        self._fetch_bootstrap()

    def _environment(self) -> str:
        values = load_env(self.config_path.parent / ".env")
        return "".join(
            f"{key}={values.get(key, '')}\n"
            for key in (
                "POWER_SECRET_ID", "POWER_SECRET_KEY", "MANAGER_API_KEY",
            )
            if key in values
        )

    def power(self) -> Power:
        settings = self.config.power_api
        if not settings:
            raise PowerError("manager.power-api 未配置")
        return create_power(
            settings.provider,
            PowerTarget(settings.instance_id, settings.instance_region),
            load_env(self.config_path.parent / ".env"),
        )

    def command(
        self, arguments: list[str], *, tty: bool = False, capture: bool = False,
    ):
        del tty
        method, path, body, job = self._translate(arguments)
        data = self._api_request(method, path, body)
        if job:
            progress = _JobProgress()
            data = self._sdk().wait_job(data["job_id"], on_event=progress)
            progress.finish()
        text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        if capture:
            import subprocess
            return subprocess.CompletedProcess(arguments, 0, stdout=text.encode(), stderr=b"")
        renderer.result(text, end="", data=True)
        return None

    @staticmethod
    def _translate(arguments: list[str]) -> tuple[str, str, dict | None, bool]:
        action = arguments[0]; pack = arguments[1] if len(arguments) > 1 else ""
        if action == "list": return "GET", "/v1/modpacks", None, False
        if action == "status": return "GET", f"/v1/modpacks/{pack}", None, False
        if action == "config": return "POST", f"/v1/modpacks/{pack}/validate", {}, False
        if action in ("start", "stop"): return "POST", f"/v1/modpacks/{pack}/{action}", {}, True
        if action in ("enable", "disable"):
            return "POST", f"/v1/modpacks/{pack}/{action}", {"now": "--now" in arguments}, True
        if action == "stop-all": return "POST", "/v1/modpacks/stop-all", {}, True
        if action == "create": return "POST", "/v1/modpacks", {"id": pack, "force": "--force" in arguments}, True
        if action == "remove": return "DELETE", f"/v1/modpacks/{pack}", {"force": "--force" in arguments}, True
        if action == "remove-container": return "DELETE", f"/v1/modpacks/{pack}/container", {}, True
        if action == "logs": return "GET", f"/v1/modpacks/{pack}/logs?lines={arguments[3] if '--lines' in arguments else 100}", None, False
        if action == "console": return "POST", f"/v1/modpacks/{pack}/console", {"command": " ".join(arguments[2:])}, False
        if action == "property":
            path = f"/v1/modpacks/{pack}/properties/{arguments[2]}"
            return ("GET", path, None, False) if len(arguments) == 3 else ("PATCH", path, {"value": arguments[3]}, False)
        if action == "backup" and pack == "list": return "GET", f"/v1/modpacks/{arguments[2]}/backups", None, False
        if action == "backup": return "POST", f"/v1/modpacks/{pack}/backups", {}, True
        if action == "restore":
            backup = next((v for v in arguments[2:] if not v.startswith("--")), None)
            return "POST", f"/v1/modpacks/{pack}/restore", {"backup": backup, "auto_restart": "--auto-restart" in arguments}, True
        raise ManagerError(f"不支持的 Manager 命令：{action}")

    def local(self, arguments: list[str], *, capture: bool = False):
        return remote.run(
            [str(SOURCE_ROOT / "manager/manager.sh"), *arguments], capture=capture,
        )

    def deploy_modpack(self, pack_id: str) -> None:
        renderer.progress(f"[整合包 1/3] 验证本地整合包：{pack_id}")
        if not re.fullmatch(r"[A-Za-z0-9-]+", pack_id):
            raise ManagerError("整合包 ID 仅允许大小写字母、数字和减号")
        local = SOURCE_ROOT / "manager/modpacks" / pack_id
        if not (local / "src").is_dir() or not (local / "config.yml").is_file():
            raise ManagerError(f"本地整合包不存在或不完整：{pack_id}")
        archive = self._source_archive(pack_id, local)
        try:
            size = archive.stat().st_size
            renderer.progress(f"[整合包 2/3] 分块上传 {size} 字节并校验 SHA-256")
            renderer.progress("[整合包 3/3] 由远端 Manager 校验并原子导入")
            progress = _JobProgress()
            self._sdk().import_archive(
                archive, force=True, on_event=progress,
            )
            progress.finish()
        finally:
            shutil.rmtree(archive.parent, ignore_errors=True)

    def import_archive(self, archive: Path, *, force: bool = False) -> None:
        progress = _JobProgress()
        self._sdk().import_archive(archive, force=force, on_event=progress)
        progress.finish()

    def export_archive(
        self, pack_id: str, destination: Path, *, source_only: bool = False,
        include_backups: bool = False, auto_stop: bool = False,
    ) -> None:
        sdk = self._sdk()
        metadata = sdk.create_export(
            pack_id, source_only=source_only,
            include_backups=include_backups, auto_stop=auto_stop,
        )
        sdk.download_export(metadata, destination)

    @staticmethod
    def _file_hash(path: Path) -> str:
        return ManagerSdk.file_sha256(path)

    def _source_archive(self, pack_id: str, source: Path) -> Path:
        temporary_root = Path(tempfile.mkdtemp(prefix="mcsp-transfer-"))
        output = temporary_root / f"{pack_id}.tgz"
        roots = ("config.yml", "src")
        files: dict[str, str] = {}
        for name in roots:
            root = source / name
            paths = [root] if root.is_file() else sorted(root.rglob("*"))
            for path in paths:
                if path.is_symlink(): raise ManagerError(f"整合包不能包含符号链接：{path.relative_to(source)}")
                if path.is_file(): files[path.relative_to(source).as_posix()] = f"sha256:{self._file_hash(path)}"
        try: name = yaml.safe_load((source / "config.yml").read_text(encoding="utf-8")).get("name", pack_id)
        except Exception as exc: raise ManagerError(f"整合包 config.yml 无效：{exc}") from exc
        manifest = {"format": 1, "pack_id": pack_id, "name": name,
                    "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "mode": "source-only", "include_backups": False, "files": files}
        manifest_path = temporary_root / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        with tarfile.open(output, "w:gz") as archive:
            archive.add(manifest_path, arcname="manifest.json")
            for name in roots: archive.add(source / name, arcname=name)
        manifest_path.unlink()
        return output

    def run(
        self, action: str, pack_id: Optional[str] = None, *,
        extra: Tuple[str, ...] = (), tty: bool = False,
    ) -> None:
        arguments = [action]
        if pack_id is not None:
            arguments.append(pack_id)
        arguments.extend(extra)
        if action == "list" or (action == "backup" and pack_id == "list"):
            result = self.command(arguments, capture=True)
            raw = result.stdout or b""
            output = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
            if output:
                renderer.result(
                    output, end="" if output.endswith("\n") else "\n", data=True,
                )
            return
        self.command(arguments, tty=tty)
