"""Manager v1 API 路由、权限、任务与统一响应。"""

from __future__ import annotations

import json
import base64
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlsplit

from .backup import BackupError, backup_id, backup_sha256
from .config import ConfigError, MANAGER_ROOT, validate_pack_id
from .docker import DockerError
from .jobs import ApiError, Job, JobStore
from .modpacks import ModpackError, ModpackManager
from .transfer import TransferStore, export_pack, import_pack


@dataclass(frozen=True)
class Request:
    method: str
    target: str
    body: bytes = b""
    identity: str = "local"
    scopes: frozenset[str] = frozenset({"*"})
    idempotency_key: str | None = None


@dataclass(frozen=True)
class Response:
    status: int
    document: dict


def error_document(exc: Exception) -> dict:
    if isinstance(exc, ApiError):
        return {"code": exc.code, "message": exc.message, "detail": exc.detail}
    mappings = (
        (ConfigError, "invalid_config"), (ModpackError, "modpack_error"),
        (DockerError, "docker_error"), (BackupError, "backup_error"),
    )
    for kind, code in mappings:
        if isinstance(exc, kind):
            return {"code": code, "message": str(exc), "detail": {}}
    return {"code": "internal_error", "message": "Manager 内部错误", "detail": {}}


class Router:
    def __init__(self, root: Path = MANAGER_ROOT, jobs: JobStore | None = None) -> None:
        self.root = root
        self.jobs = jobs or JobStore(root, error_document=error_document)
        self.jobs.error_document = error_document
        self.transfers = TransferStore(root)
        self.routes: dict[tuple[str, str], Callable[[Request, str], Response]] = {
            ("GET", "/v1/health"): self._health,
            ("GET", "/v1/modpacks"): self._list_modpacks,
            ("POST", "/v1/modpacks"): self._create_modpack,
            ("POST", "/v1/imports"): self._create_import,
            ("POST", "/v1/modpacks/stop-all"): self._stop_all_route,
        }

    @staticmethod
    def _json(request: Request) -> dict:
        if not request.body:
            return {}
        try:
            value = json.loads(request.body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError(400, "invalid_json", "请求体必须是 JSON 对象") from exc
        if not isinstance(value, dict):
            raise ApiError(400, "invalid_json", "请求体必须是 JSON 对象")
        return value

    @staticmethod
    def _require(request: Request, scope: str) -> None:
        if "*" not in request.scopes and scope not in request.scopes:
            raise ApiError(403, "permission_denied", "当前凭据无权执行此操作")

    @staticmethod
    def _ok(request_id: str, data: object, status: int = 200) -> Response:
        return Response(status, {"ok": True, "request_id": request_id, "data": data})

    def _manager(self, job: Job | None = None) -> ModpackManager:
        return ModpackManager(self.root, events=job.emit if job else None) if job else ModpackManager(self.root)

    def _job(
        self, request: Request, request_id: str, operation: str, scope: str,
        action: Callable[[Job], object], pack_id: str | None = None,
        resource: bool = False,
    ) -> Response:
        self._require(request, scope)
        job = self.jobs.submit(request, request_id, operation, action,
                               pack_id=pack_id, resource=resource)
        return self._ok(request_id, {"job_id": job.job_id, "state": job.state}, 202)

    def _health(self, request: Request, request_id: str) -> Response:
        self._require(request, "health")
        return self._ok(request_id, {"status": "ok"})

    def _list_modpacks(self, request: Request, request_id: str) -> Response:
        self._require(request, "modpacks:read")
        return self._ok(request_id, {"modpacks": self._manager().summaries()})

    def _create_modpack(self, request: Request, request_id: str) -> Response:
        body = self._json(request)
        pack_id = validate_pack_id(body.get("id"))
        return self._job(
            request, request_id, "create", "modpacks:write",
            lambda job: {
                "path": str(
                    self._manager(job).create(
                        pack_id, force=body.get("force") is True,
                    )
                )
            },
            pack_id, resource=True,
        )

    def _create_import(self, request: Request, request_id: str) -> Response:
        self._require(request, "transfers:write")
        body = self._json(request)
        return self._ok(
            request_id,
            self.transfers.create_upload(
                body.get("pack_id"), body.get("size"), body.get("sha256"),
                force=body.get("force") is True,
            ),
            201,
        )

    def _stop_all_route(self, request: Request, request_id: str) -> Response:
        return self._job(
            request, request_id, "stop-all", "modpacks:stop",
            lambda job: self._stop_all(job), resource=True,
        )

    def dispatch(self, request: Request) -> Response:
        request_id = uuid.uuid4().hex
        try:
            return self._dispatch(request, request_id)
        except ApiError as exc:
            return Response(exc.status, {"ok": False, "request_id": request_id, "error": error_document(exc)})
        except (ConfigError, ModpackError, DockerError, BackupError) as exc:
            return Response(400, {"ok": False, "request_id": request_id, "error": error_document(exc)})
        except Exception as exc:
            return Response(500, {"ok": False, "request_id": request_id, "error": error_document(exc)})

    def _dispatch(self, request: Request, request_id: str) -> Response:
        parsed = urlsplit(request.target)
        path = parsed.path.rstrip("/") or "/"
        parts = path.strip("/").split("/")
        method = request.method.upper()
        handler = self.routes.get((method, path))
        if handler is not None:
            return handler(request, request_id)
        if len(parts) == 3 and parts[:2] == ["v1", "jobs"] and method == "GET":
            self._require(request, "jobs:read")
            query = parse_qs(parsed.query)
            try:
                after = int(query.get("after", ["0"])[0])
                wait = float(query.get("wait", ["0"])[0])
            except ValueError as exc:
                raise ApiError(400, "invalid_argument", "after 或 wait 参数无效") from exc
            if after < 0 or wait < 0:
                raise ApiError(400, "invalid_argument", "after 或 wait 不能为负数")
            return self._ok(request_id, self.jobs.get(parts[2]).document(after, wait))
        if len(parts) == 5 and parts[:2] == ["v1", "transfers"] and parts[3] == "chunks":
            transfer_id = parts[2]
            try: index = int(parts[4])
            except ValueError as exc: raise ApiError(400, "invalid_argument", "分块序号无效") from exc
            if method == "PUT":
                self._require(request, "transfers:write")
                digest = parse_qs(parsed.query).get("sha256", [""])[0]
                return self._ok(request_id, self.transfers.put_chunk(transfer_id, index, request.body, digest))
            if method == "GET":
                self._require(request, "transfers:read")
                content, metadata = self.transfers.get_chunk(transfer_id, index)
                return self._ok(request_id, {**metadata, "content": base64.b64encode(content).decode("ascii")})
        if len(parts) == 4 and parts[:2] == ["v1", "transfers"] and parts[3] == "complete" and method == "POST":
            self._require(request, "transfers:write")
            transfer_id = parts[2]
            return self._job(request, request_id, "import", "transfers:write",
                             lambda j: self._complete_import(transfer_id, j), resource=True)
        if len(parts) < 3 or parts[:2] != ["v1", "modpacks"]:
            raise ApiError(404, "not_found", "接口不存在")
        pack_id = validate_pack_id(parts[2])
        manager = self._manager()
        if len(parts) == 3 and method == "GET":
            self._require(request, "modpacks:read")
            return self._ok(request_id, manager.details(pack_id))
        if len(parts) == 3 and method == "DELETE":
            body = self._json(request)
            return self._job(request, request_id, "remove", "modpacks:delete", lambda j: (
                self._manager(j).remove(pack_id, force=body.get("force") is True) or {"id": pack_id}
            ), pack_id, resource=True)
        action = parts[3] if len(parts) > 3 else ""
        if action == "validate" and method == "POST":
            self._require(request, "modpacks:read")
            return self._ok(request_id, {"compose": str(manager.configure(pack_id))})
        if action in ("start", "stop") and method == "POST":
            return self._job(request, request_id, action, f"modpacks:{action}", lambda j: (
                getattr(self._manager(j), action)(pack_id) or {"id": pack_id}
            ), pack_id, resource=action == "start")
        if action in ("enable", "disable") and method == "POST":
            body = self._json(request); enabled = action == "enable"
            return self._job(request, request_id, action, "modpacks:write", lambda j: (
                self._manager(j).set_enabled(pack_id, enabled, now=body.get("now") is True)
                or {"id": pack_id, "enabled": enabled}
            ), pack_id, resource=True)
        if action == "container" and method == "DELETE":
            return self._job(request, request_id, "remove-container", "modpacks:write", lambda j: (
                self._manager(j).remove_container(pack_id) or {"id": pack_id}
            ), pack_id)
        if action == "console" and method == "POST":
            self._require(request, "console:write"); command = self._json(request).get("command")
            if not isinstance(command, str): raise ApiError(400, "invalid_argument", "command 必须是字符串")
            manager.console(pack_id, command); return self._ok(request_id, {"sent": True})
        if action == "logs" and method == "GET":
            self._require(request, "logs:read")
            try: lines = int(parse_qs(parsed.query).get("lines", ["100"])[0])
            except ValueError as exc: raise ApiError(400, "invalid_argument", "lines 参数无效") from exc
            if not 0 <= lines <= 10000: raise ApiError(400, "invalid_argument", "lines 必须在 0 到 10000 之间")
            return self._ok(request_id, {"text": manager.log_text(pack_id, lines=lines)})
        if action == "properties" and len(parts) == 5:
            key = parts[4]
            if method == "GET":
                self._require(request, "modpacks:read"); value, file = manager.get_property(pack_id, key)
                return self._ok(request_id, {"key": key, "value": value, "file": str(file)})
            if method == "PATCH":
                self._require(request, "modpacks:write"); value = self._json(request).get("value")
                if not isinstance(value, str): raise ApiError(400, "invalid_argument", "value 必须是字符串")
                file, running = manager.set_property(pack_id, key, value)
                return self._ok(request_id, {"key": key, "value": value, "file": str(file), "restart_required": running})
        if action == "backups":
            if method == "GET":
                self._require(request, "backups:read")
                values = [{"id": backup_id(p), "sha256": backup_sha256(p), "name": p.name, "size": p.stat().st_size,
                           "created_at": datetime.fromtimestamp(p.stat().st_mtime).astimezone().isoformat()}
                          for p in manager.backups(pack_id)]
                return self._ok(request_id, {"backups": values})
            if method == "POST":
                return self._job(request, request_id, "backup", "backups:write", lambda j: {
                    "path": str(self._manager(j).backup(pack_id))
                }, pack_id)
        if action == "restore" and method == "POST":
            body = self._json(request)
            return self._job(request, request_id, "restore", "backups:write", lambda j: {
                "backup": str(self._manager(j).restore(pack_id, body.get("backup"), auto_restart=body.get("auto_restart") is True))
            }, pack_id, resource=True)
        if action == "exports" and method == "POST":
            body = self._json(request)
            return self._job(request, request_id, "export", "transfers:write",
                             lambda j: self._export(pack_id, body, j), pack_id, resource=True)
        raise ApiError(404, "not_found", "接口不存在")

    def _complete_import(self, transfer_id: str, job: Job) -> dict:
        archive, metadata = self.transfers.assemble(transfer_id)
        manager = self._manager(job)
        path = import_pack(manager, metadata["pack_id"], archive, force=metadata["force"])
        return {"id": metadata["pack_id"], "path": str(path)}

    def _stop_all(self, job: Job) -> dict:
        stopped, failed = self._manager(job).stop_all()
        if failed:
            raise ModpackError("部分整合包未能安全停止：" + "；".join(f"{name}: {message}" for name, message in failed))
        return {"stopped": stopped}

    def _export(self, pack_id: str, body: dict, job: Job) -> dict:
        manager = self._manager(job); mode = body.get("mode", "full")
        if mode == "full" and manager.docker.running(manager.spec(pack_id)):
            if body.get("auto_stop") is not True:
                raise ModpackError("运行中的整合包不能全量导出；请先停服或使用 auto_stop=true")
            manager.stop(pack_id)
        with tempfile.TemporaryDirectory(prefix="mcsp-pack-") as raw:
            archive = export_pack(
                pack_id, manager.packs_dir, mode=mode,
                include_backups=body.get("include_backups") is True,
                destination=Path(raw) / f"{pack_id}.tgz",
            )
            return self.transfers.create_download(pack_id, archive)
