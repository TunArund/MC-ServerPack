"""Manager 后台任务执行、互斥与事件持久化。"""

from __future__ import annotations

import contextlib
import json
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Callable, Protocol


LOGGER = logging.getLogger("manager.business")


class ApiError(RuntimeError):
    def __init__(self, status: int, code: str, message: str, detail: dict | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.detail = detail or {}


class JobRequest(Protocol):
    method: str
    target: str
    body: bytes
    identity: str
    idempotency_key: str | None


def _internal_error(exc: Exception) -> dict:
    del exc
    return {"code": "internal_error", "message": "Manager 内部错误", "detail": {}}


class Job:
    def __init__(
        self, root: Path, request_id: str, operation: str, *,
        job_id: str | None = None, initialize: bool = True, identity: str = "",
        idempotency_key: str | None = None, fingerprint: bytes = b"",
    ) -> None:
        self.job_id = job_id or uuid.uuid4().hex
        self.request_id = request_id
        self.operation = operation
        self.state = "queued"
        self.result: object | None = None
        self.error: dict | None = None
        self.identity = identity
        self.idempotency_key = idempotency_key
        self.fingerprint = fingerprint
        self.events: list[dict] = []
        self.path = root / "data/jobs" / f"{self.job_id}.jsonl"
        self.status_path = root / "data/jobs" / f"{self.job_id}.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.condition = threading.Condition()
        if initialize:
            self.emit(
                "job", 0, 1, "任务已进入队列", {"operation": operation},
                code="job-queued",
            )
            self.persist()

    def persist(self) -> None:
        value = {
            "job_id": self.job_id, "request_id": self.request_id,
            "operation": self.operation, "state": self.state,
            "result": self.result, "error": self.error,
            "identity": self.identity, "idempotency_key": self.idempotency_key,
            "fingerprint": self.fingerprint.hex(),
        }
        temporary = self.status_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        temporary.replace(self.status_path)

    def emit(
        self, stage: str, current: int, total: int, message: str,
        detail: dict[str, object] | None = None, *, code: str | None = None,
        event_type: str = "progress",
    ) -> None:
        with self.condition:
            event = {
                "seq": len(self.events) + 1,
                "time": datetime.now().astimezone().isoformat(timespec="seconds"),
                "level": "info", "type": event_type,
                "code": code or f"{stage}-progress",
                "stage": stage, "current": current, "total": total,
                "message": message, "detail": detail or {},
            }
            self.events.append(event)
            with self.path.open("a", encoding="utf-8") as output:
                output.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
            self.condition.notify_all()

    def document(self, after: int = 0, wait: float = 0) -> dict:
        deadline = time.monotonic() + min(max(wait, 0), 30)
        with self.condition:
            while len(self.events) <= after and self.state in ("queued", "running"):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.condition.wait(remaining)
            return {
                "job_id": self.job_id, "request_id": self.request_id,
                "operation": self.operation, "state": self.state,
                "events": self.events[after:], "result": self.result, "error": self.error,
            }


class JobStore:
    def __init__(
        self, root: Path, workers: int = 4, *,
        error_document: Callable[[Exception], dict] = _internal_error,
    ) -> None:
        self.root = root
        self.error_document = error_document
        self.executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="manager-job")
        self.jobs: dict[str, Job] = {}
        self.idempotency: dict[tuple[str, str], tuple[bytes, str]] = {}
        self.lock = threading.Lock()
        self.global_job_lock = threading.Lock()
        self.resource_lock = threading.Lock()
        self.pack_locks: dict[str, threading.Lock] = {}
        self._load()

    def _load(self) -> None:
        directory = self.root / "data/jobs"
        if not directory.is_dir():
            return
        for path in directory.glob("*.json"):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                job = Job(
                    self.root, value["request_id"], value["operation"],
                    job_id=value["job_id"], initialize=False,
                    identity=value.get("identity", ""),
                    idempotency_key=value.get("idempotency_key"),
                    fingerprint=bytes.fromhex(value.get("fingerprint", "")),
                )
                job.state = value["state"]
                if job.state in ("queued", "running"):
                    job.state = "failed"
                    job.error = {
                        "code": "service_restarted",
                        "message": "Manager 重启，中断了未完成任务", "detail": {},
                    }
                else:
                    job.result, job.error = value.get("result"), value.get("error")
                if job.path.is_file():
                    for line in job.path.read_text(encoding="utf-8").splitlines():
                        event = json.loads(line)
                        if isinstance(event, dict):
                            job.events.append(event)
                job.persist()
                self.jobs[job.job_id] = job
                if job.idempotency_key:
                    self.idempotency[(job.identity, job.idempotency_key)] = (
                        job.fingerprint, job.job_id,
                    )
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue

    def submit(
        self, request: JobRequest, request_id: str, operation: str,
        action: Callable[[Job], object], *, pack_id: str | None = None,
        resource: bool = False,
    ) -> Job:
        fingerprint = request.method.encode() + b"\0" + request.target.encode() + b"\0" + request.body
        with self.lock:
            if request.idempotency_key:
                key = (request.identity, request.idempotency_key)
                previous = self.idempotency.get(key)
                if previous:
                    if previous[0] != fingerprint:
                        raise ApiError(409, "idempotency_conflict", "幂等键已用于不同请求")
                    return self.jobs[previous[1]]
            job = Job(
                self.root, request_id, operation, identity=request.identity,
                idempotency_key=request.idempotency_key, fingerprint=fingerprint,
            )
            self.jobs[job.job_id] = job
            if request.idempotency_key:
                self.idempotency[(request.identity, request.idempotency_key)] = (
                    fingerprint, job.job_id,
                )
            pack_lock = (
                self.pack_locks.setdefault(pack_id, threading.Lock())
                if pack_id else self.global_job_lock
            )

        def run() -> None:
            job.state = "running"
            job.emit("job", 0, 1, "任务开始执行", None, code="job-running")
            job.persist()
            try:
                with (self.resource_lock if resource else contextlib.nullcontext()):
                    with pack_lock:
                        job.result = action(job)
                job.state = "succeeded"
                job.emit(
                    "job", 1, 1, "任务执行完成", None,
                    code="job-succeeded", event_type="result",
                )
                LOGGER.info(
                    "request_id=%s job_id=%s operation=%s state=succeeded",
                    job.request_id, job.job_id, job.operation,
                )
            except Exception as exc:
                LOGGER.exception(
                    "request_id=%s job_id=%s operation=%s internal failure",
                    job.request_id, job.job_id, job.operation,
                )
                job.state = "failed"
                job.error = self.error_document(exc)
                event = {
                    "seq": len(job.events) + 1,
                    "time": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "level": "error", "type": "error",
                    "code": job.error["code"],
                    "stage": "job", "current": 1, "total": 1,
                    "message": job.error["message"], "detail": {},
                }
                with job.condition:
                    job.events.append(event)
                    with job.path.open("a", encoding="utf-8") as output:
                        output.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
                    job.condition.notify_all()
                LOGGER.error(
                    "request_id=%s job_id=%s operation=%s state=failed code=%s",
                    job.request_id, job.job_id, job.operation, job.error["code"],
                )
            finally:
                job.persist()

        self.executor.submit(run)
        return job

    def get(self, job_id: str) -> Job:
        try:
            return self.jobs[job_id]
        except KeyError as exc:
            raise ApiError(404, "job_not_found", f"任务不存在：{job_id}") from exc

    def close(self) -> None:
        self.executor.shutdown(wait=True, cancel_futures=True)
