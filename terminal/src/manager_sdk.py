"""Manager HTTP API 客户端与签名约定。

这是唯一的客户端实现：Manager 维护本文件，再原样复制到 Terminal 的
``lib/manager_sdk.py`` 与 Monitor 的 ``monitor/src/manager_sdk.py``。Manager 服务端
本身不发起 HTTP 调用，但运行时直接使用这里的 :func:`signature` 校验请求，集成测试
也用 :class:`ManagerSdk` 走完整的 TLS/HMAC 往返，避免两端签名逻辑漂移。文件只依赖
Python 标准库，不导入项目内其他模块。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import ssl
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


API_PATH = "/v1/modpacks"
STOP_ALL_PATH = "/v1/modpacks/stop-all"
PACK_ID_RE = re.compile(r"[A-Za-z0-9-]+")
MODPACK_STATES = frozenset({
    "healthy", "starting", "unhealthy", "running", "stopped", "exited",
    "unknown", "maintenance", "created", "restarting", "removing", "paused",
    "dead",
})


class ManagerSdkError(RuntimeError):
    pass


def format_job_event(event: dict) -> str | None:
    """三个入口共用的稳定 Job 事件文本规则。"""
    message = event.get("message")
    if not isinstance(message, str) or not message:
        return None
    stage = event.get("stage", "job")
    current = event.get("current", 0)
    total = event.get("total", 0)
    line = f"[{stage} {current}/{total}] {message}"
    detail = event.get("detail")
    tail = detail.get("tail") if isinstance(detail, dict) else None
    return f"{line}；{tail}" if isinstance(tail, str) and tail else line


def signature(
    secret: str, method: str, target: str, timestamp: str, nonce: str,
    body: bytes,
) -> str:
    """服务端和客户端共用的 HMAC 规范化与签名实现。"""
    canonical = "\n".join((
        method.upper(), target, timestamp, nonce,
        hashlib.sha256(body).hexdigest(),
    ))
    return hmac.new(
        secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256,
    ).hexdigest()


@dataclass(frozen=True)
class PublicModpack:
    pack_id: str
    name: str
    state: str
    port: int
    address: str


class ManagerSdk:
    def __init__(
        self, host: str, port: int, secret: str, *,
        identity: str = "admin", ca: str | Path | None = None,
        timeout: float = 35, job_timeout: float | None = None,
    ) -> None:
        if not host:
            raise ManagerSdkError("Manager API 地址未配置")
        if not 1 <= port <= 65535:
            raise ManagerSdkError("Manager API 端口无效")
        if len(secret) < 32:
            raise ManagerSdkError("MANAGER_API_KEY 至少需要 32 个字符")
        if identity not in ("admin", "monitor"):
            raise ManagerSdkError(f"未知的 Manager 调用身份：{identity}")
        self.host = host
        self.port = port
        self.secret = secret
        self.identity = identity
        self.ca = Path(ca) if ca else None
        self.timeout = timeout
        self.job_timeout = job_timeout
        self.base_url = f"https://{host}:{port}"

    def _context(self) -> ssl.SSLContext:
        if self.ca is not None:
            return ssl.create_default_context(cafile=str(self.ca))
        return ssl.create_default_context()

    def _opener(self) -> urllib.request.OpenerDirector:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=self._context()),
        )

    def _signature(
        self, method: str, target: str, timestamp: str, nonce: str, body: bytes,
    ) -> str:
        return signature(
            self.secret, method, target, timestamp, nonce, body,
        )

    def request(
        self, method: str, target: str, body: dict | bytes | None = None, *,
        content_type: str | None = None,
        idempotency_key: str | None = None,
        timeout: float | None = None,
    ) -> dict:
        raw = (
            body if isinstance(body, bytes) else (
                json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                if body is not None else b""
            )
        )
        timestamp = str(int(time.time()))
        nonce = secrets.token_hex(16)
        headers = {
            "Accept": "application/json",
            "X-MCSP-Key-Id": self.identity,
            "X-MCSP-Timestamp": timestamp,
            "X-MCSP-Nonce": nonce,
            "X-MCSP-Signature": self._signature(
                method, target, timestamp, nonce, raw,
            ),
        }
        if body is not None:
            headers["Content-Type"] = content_type or "application/json"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        request = urllib.request.Request(
            self.base_url + target,
            data=raw if body is not None else None,
            method=method,
            headers=headers,
        )
        try:
            with self._opener().open(
                request,
                timeout=self.timeout if timeout is None else timeout,
            ) as response:
                document = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            try:
                message = json.loads(exc.read()).get("error", {}).get("message")
            except (OSError, ValueError, AttributeError):
                message = None
            raise ManagerSdkError(
                message or f"Manager API 返回 HTTP {exc.code}",
            ) from exc
        except (OSError, ValueError) as exc:
            raise ManagerSdkError(f"Manager API 不可用：{exc}") from exc
        if not isinstance(document, dict) or document.get("ok") is not True:
            message = document.get("error", {}).get("message") if isinstance(document, dict) else None
            raise ManagerSdkError(message or "Manager API 响应格式无效")
        data = document.get("data")
        if not isinstance(data, dict):
            raise ManagerSdkError("Manager API 响应格式无效")
        return data

    def wait_job(
        self, job_id: str,
        on_event: Callable[[dict], None] | None = None,
    ) -> object:
        sequence = 0
        deadline = (
            time.monotonic() + self.job_timeout
            if self.job_timeout is not None else None
        )
        while True:
            data = self.request(
                "GET", f"/v1/jobs/{job_id}?after={sequence}&wait=20",
                timeout=max(self.timeout, 35),
            )
            events = data.get("events", [])
            if not isinstance(events, list):
                raise ManagerSdkError("Manager 任务事件格式无效")
            for event in events:
                if not isinstance(event, dict):
                    raise ManagerSdkError("Manager 任务事件格式无效")
                try:
                    sequence = max(sequence, int(event.get("seq", sequence)))
                except (TypeError, ValueError) as exc:
                    raise ManagerSdkError("Manager 任务事件格式无效") from exc
                if on_event is not None:
                    on_event(event)
            state = data.get("state")
            if state == "succeeded":
                return data.get("result")
            if state in ("failed", "cancelled"):
                error = data.get("error")
                message = error.get("message") if isinstance(error, dict) else None
                raise ManagerSdkError(message or "Manager 任务失败")
            if state not in ("queued", "running"):
                raise ManagerSdkError("Manager 任务状态无效")
            if deadline is not None and time.monotonic() >= deadline:
                raise ManagerSdkError("等待 Manager 任务超时")

    def job(
        self, method: str, target: str, body: dict | bytes | None = None, *,
        on_event: Callable[[dict], None] | None = None,
    ) -> object:
        data = self.request(
            method, target, body, idempotency_key=secrets.token_hex(16),
        )
        job_id = data.get("job_id")
        if not isinstance(job_id, str) or not job_id:
            raise ManagerSdkError("Manager API 未返回任务编号")
        return self.wait_job(job_id, on_event)

    def list_modpacks(self) -> list[dict]:
        data = self.request("GET", API_PATH)
        modpacks = data.get("modpacks")
        if not isinstance(modpacks, list):
            raise ManagerSdkError("Manager API 返回格式无效")
        return modpacks

    def public_modpacks(self, public_host: str) -> tuple[PublicModpack, ...]:
        if not public_host:
            raise ManagerSdkError("Manager 云主机没有公网 IP")
        result = []
        for index, item in enumerate(self.list_modpacks()):
            if not isinstance(item, dict):
                raise ManagerSdkError(f"modpacks[{index}] 格式无效")
            try:
                pack_id = item["id"]
                name = item["name"]
                state = item["state"]
                port = item["port"]
                address = f"{public_host}:{port}"
            except KeyError as exc:
                raise ManagerSdkError(f"modpacks[{index}] 缺少字段") from exc
            if (
                not all(isinstance(value, str) and value for value in (
                    pack_id, name, state, address,
                ))
                or state not in MODPACK_STATES
                or type(port) is not int or not 1 <= port <= 65535
            ):
                raise ManagerSdkError(f"modpacks[{index}] 字段无效")
            result.append(PublicModpack(pack_id, name, state, port, address))
        return tuple(result)

    def list(self, public_host: str) -> tuple[PublicModpack, ...]:
        return self.public_modpacks(public_host)

    def status(self, pack_id: str) -> dict:
        return self.request("GET", f"/v1/modpacks/{pack_id}")

    def validate(self, pack_id: str) -> dict:
        return self.request("POST", f"/v1/modpacks/{pack_id}/validate", {})

    def start(self, pack_id: str, on_event=None) -> object:
        return self.job("POST", f"/v1/modpacks/{pack_id}/start", {}, on_event=on_event)

    def stop(self, pack_id: str, on_event=None) -> object:
        return self.job("POST", f"/v1/modpacks/{pack_id}/stop", {}, on_event=on_event)

    def stop_all(self, on_event=None) -> object:
        return self.job("POST", STOP_ALL_PATH, {}, on_event=on_event)

    def enable(self, pack_id: str, *, now: bool = False, on_event=None) -> object:
        return self.job(
            "POST", f"/v1/modpacks/{pack_id}/enable", {"now": now},
            on_event=on_event,
        )

    def disable(self, pack_id: str, *, now: bool = False, on_event=None) -> object:
        return self.job(
            "POST", f"/v1/modpacks/{pack_id}/disable", {"now": now},
            on_event=on_event,
        )

    def create(self, pack_id: str, *, force: bool = False, on_event=None) -> object:
        return self.job(
            "POST", API_PATH, {"id": pack_id, "force": force},
            on_event=on_event,
        )

    def remove(self, pack_id: str, *, force: bool = False, on_event=None) -> object:
        return self.job(
            "DELETE", f"/v1/modpacks/{pack_id}", {"force": force},
            on_event=on_event,
        )

    def remove_container(self, pack_id: str, on_event=None) -> object:
        return self.job(
            "DELETE", f"/v1/modpacks/{pack_id}/container", {},
            on_event=on_event,
        )

    def logs(self, pack_id: str, *, lines: int = 100) -> str:
        if lines < 0:
            raise ManagerSdkError("日志行数不能为负数")
        data = self.request(
            "GET", f"/v1/modpacks/{pack_id}/logs?lines={lines}",
        )
        text = data.get("text")
        if not isinstance(text, str):
            raise ManagerSdkError("Manager API 日志格式无效")
        return text

    def console(self, pack_id: str, command: str) -> dict:
        return self.request(
            "POST", f"/v1/modpacks/{pack_id}/console", {"command": command},
        )

    def get_property(self, pack_id: str, key: str) -> dict:
        quoted = urllib.parse.quote(key, safe="")
        return self.request(
            "GET", f"/v1/modpacks/{pack_id}/properties/{quoted}",
        )

    def set_property(self, pack_id: str, key: str, value: str) -> dict:
        quoted = urllib.parse.quote(key, safe="")
        return self.request(
            "PATCH", f"/v1/modpacks/{pack_id}/properties/{quoted}",
            {"value": value},
        )

    def list_backups(self, pack_id: str) -> dict:
        return self.request("GET", f"/v1/modpacks/{pack_id}/backups")

    def backup(self, pack_id: str, on_event=None) -> object:
        return self.job(
            "POST", f"/v1/modpacks/{pack_id}/backups", {}, on_event=on_event,
        )

    def restore(
        self, pack_id: str, backup: str | None = None, *,
        auto_restart: bool = False, on_event=None,
    ) -> object:
        return self.job(
            "POST", f"/v1/modpacks/{pack_id}/restore",
            {"backup": backup, "auto_restart": auto_restart},
            on_event=on_event,
        )

    def create_import(
        self, pack_id: str, size: int, sha256: str, *, force: bool = False,
    ) -> dict:
        return self.request("POST", "/v1/imports", {
            "pack_id": pack_id, "size": size, "sha256": sha256, "force": force,
        })

    def put_chunk(
        self, transfer_id: str, index: int, content: bytes, sha256: str,
    ) -> dict:
        quoted = urllib.parse.quote(sha256, safe="")
        return self.request(
            "PUT",
            f"/v1/transfers/{transfer_id}/chunks/{index}?sha256={quoted}",
            content, content_type="application/octet-stream",
        )

    def complete_import(self, transfer_id: str, on_event=None) -> object:
        return self.job(
            "POST", f"/v1/transfers/{transfer_id}/complete", {},
            on_event=on_event,
        )

    def create_export(
        self, pack_id: str, *, source_only: bool = False,
        include_backups: bool = False, auto_stop: bool = False,
    ) -> dict:
        return self.job("POST", f"/v1/modpacks/{pack_id}/exports", {
            "mode": "source-only" if source_only else "full",
            "include_backups": include_backups,
            "auto_stop": auto_stop,
        })

    def download_export(self, metadata: dict, destination: Path) -> None:
        size = metadata.get("size")
        digest = metadata.get("sha256")
        transfer_id = metadata.get("transfer_id")
        if (
            type(size) is not int or size < 0
            or not isinstance(digest, str)
            or not isinstance(transfer_id, str)
        ):
            raise ManagerSdkError("Manager 导出信息格式无效")
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        try:
            with temporary.open("wb") as output:
                index = 0
                while output.tell() < size:
                    chunk = self.request(
                        "GET", f"/v1/transfers/{transfer_id}/chunks/{index}",
                    )
                    content = base64.b64decode(chunk["content"], validate=True)
                    if hashlib.sha256(content).hexdigest() != chunk.get("sha256"):
                        raise ManagerSdkError(f"下载分块校验失败：{index}")
                    output.write(content)
                    index += 1
            if (
                temporary.stat().st_size != size
                or self.file_sha256(temporary) != digest
            ):
                raise ManagerSdkError("下载归档完整性校验失败")
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)

    def import_archive(self, archive: Path, *, force: bool = False, on_event=None) -> None:
        archive = Path(archive)
        if not archive.is_file():
            raise ManagerSdkError(f"归档不存在：{archive}")
        with tarfile.open(archive, "r:gz") as source:
            try:
                manifest = json.load(source.extractfile("manifest.json"))
            except Exception as exc:
                raise ManagerSdkError(f"归档 manifest.json 无效：{exc}") from exc
        pack_id = manifest.get("pack_id") if isinstance(manifest, dict) else None
        if not isinstance(pack_id, str) or not PACK_ID_RE.fullmatch(pack_id):
            raise ManagerSdkError("归档中的整合包 ID 无效")
        transfer = self.create_import(
            pack_id, archive.stat().st_size, self.file_sha256(archive),
            force=force,
        )
        with archive.open("rb") as source:
            index = 0
            while content := source.read(transfer["chunk_size"]):
                self.put_chunk(
                    transfer["transfer_id"], index, content,
                    hashlib.sha256(content).hexdigest(),
                )
                index += 1
        self.complete_import(transfer["transfer_id"], on_event=on_event)

    @staticmethod
    def file_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with Path(path).open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()
