"""Manager 常驻控制服务进程入口。"""

from __future__ import annotations

import hmac
import json
import logging
import os
import socketserver
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import schedule
from .config import ConfigError, MANAGER_ROOT, load_api_settings, load_limits
from .manager_sdk import signature
from .modpacks import ModpackManager
from .observability_sdk import configure_logging
from .router import Request, Response, Router


LOGGER = logging.getLogger("manager.service")
API_LOGGER = logging.getLogger("manager.api")
SCHEDULE_LOGGER = logging.getLogger("manager.schedule")
MAX_BODY = 5 * 1024 * 1024
MAX_CLOCK_SKEW = 60
ADMIN_SCOPES = frozenset({"*"})
MONITOR_SCOPES = frozenset({
    "health", "modpacks:read", "modpacks:write", "logs:read", "jobs:read",
    "modpacks:stop",
})


class ServiceError(RuntimeError):
    pass


class Nonces:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], int] = {}
        self.lock = threading.Lock()

    def accept(self, identity: str, nonce: str, timestamp: int) -> bool:
        now = int(time.time())
        with self.lock:
            self.values = {key: value for key, value in self.values.items() if now - value <= MAX_CLOCK_SKEW}
            key = (identity, nonce)
            if key in self.values:
                return False
            self.values[key] = timestamp
            return True


def _handler(router: Router, keys: dict[str, tuple[str, frozenset[str]]], local: bool, nonces: Nonces):
    class Handler(BaseHTTPRequestHandler):
        server_version = "MCSP-Manager/1"

        def do_GET(self) -> None: self._handle()  # noqa: N802
        def do_POST(self) -> None: self._handle()  # noqa: N802
        def do_PATCH(self) -> None: self._handle()  # noqa: N802
        def do_PUT(self) -> None: self._handle()  # noqa: N802
        def do_DELETE(self) -> None: self._handle()  # noqa: N802

        def _handle(self) -> None:
            started = time.monotonic()
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self._reply(Response(400, {"ok": False, "error": {"code": "invalid_length", "message": "Content-Length 无效", "detail": {}}}))
                return
            if length < 0 or length > MAX_BODY:
                self._reply(Response(413, {"ok": False, "error": {"code": "body_too_large", "message": "请求体过大", "detail": {}}}))
                return
            body = self.rfile.read(length)
            identity, scopes = ("local", ADMIN_SCOPES) if local else self._authenticate(body)
            if scopes is None:
                return
            request = Request(
                self.command, self.path, body, identity, scopes,
                self.headers.get("Idempotency-Key"),
            )
            response = router.dispatch(request)
            self._reply(response)
            API_LOGGER.info(
                "API request completed", extra={
                    "event": "api-request-completed",
                    "request_id": response.document.get("request_id", "-"),
                    "identity": identity, "method": self.command,
                    "path": self.path.split("?", 1)[0], "status": response.status,
                    "elapsed_ms": int((time.monotonic() - started) * 1000),
                },
            )

        def _authenticate(self, body: bytes) -> tuple[str, frozenset[str] | None]:
            identity = self.headers.get("X-MCSP-Key-Id", "")
            timestamp = self.headers.get("X-MCSP-Timestamp", "")
            nonce = self.headers.get("X-MCSP-Nonce", "")
            supplied = self.headers.get("X-MCSP-Signature", "")
            entry = keys.get(identity)
            try:
                requested = int(timestamp)
            except ValueError:
                entry = None
                requested = 0
            valid = entry is not None and abs(int(time.time()) - requested) <= MAX_CLOCK_SKEW
            expected = signature(entry[0], self.command, self.path, timestamp, nonce, body) if entry else "0" * 64
            valid = valid and len(nonce) >= 16 and hmac.compare_digest(supplied, expected)
            valid = valid and nonces.accept(identity, nonce, requested)
            if not valid:
                self._reply(Response(401, {"ok": False, "error": {"code": "invalid_authentication", "message": "认证失败", "detail": {}}}))
                return identity or "unknown", None
            return identity, entry[1]

        def _reply(self, response: Response) -> None:
            if "request_id" not in response.document:
                response.document["request_id"] = os.urandom(16).hex()
            payload = json.dumps(response.document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, pattern: str, *args) -> None:
            return

    return Handler


class ThreadingUnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def _logging(root: Path) -> None:
    directory = root / "data/logs"
    # 生命周期、业务、Job 与调度统一进入 manager/service.log；访问日志单独保留。
    configure_logging({
        "manager": directory / "service.log",
        "manager.api": directory / "api.log",
    }, stream=True)


def _keys() -> dict[str, tuple[str, frozenset[str]]]:
    value = os.environ.get("MANAGER_API_KEY", "").strip()
    if not value:
        return {}
    if len(value) < 32:
        raise ServiceError("MANAGER_API_KEY 至少需要 32 个字符")
    # 单密钥设计：Terminal 与 Monitor 共用同一个密钥；调用身份仍按 header
    # 区分，权限 scope 保留。
    return {
        "admin": (value, ADMIN_SCOPES),
        "monitor": (value, MONITOR_SCOPES),
    }


def _schedule_loop(root: Path, stopped: threading.Event) -> None:
    while not stopped.is_set():
        try:
            report = schedule.run_all(ModpackManager(root))
            if report.failures:
                SCHEDULE_LOGGER.error("调度部分失败：%s", report.failures)
            elif report.backups or report.stopped or report.poweroff:
                SCHEDULE_LOGGER.info("调度完成：backups=%s stopped=%s poweroff=%s",
                                     len(report.backups), report.stopped, report.poweroff)
        except Exception:
            SCHEDULE_LOGGER.exception("聚合调度失败")
        stopped.wait(60)


def _reconcile(root: Path) -> None:
    manager = ModpackManager(root)
    for spec in manager.specs():
        if manager.enabled(spec.pack_id) and not manager.maintenance(spec.pack_id):
            try:
                manager.start(spec.pack_id)
            except Exception:
                LOGGER.exception("自动启动失败：%s", spec.pack_id)


def serve(root: Path = MANAGER_ROOT) -> None:
    settings = load_api_settings(root / "data/config.yml")
    limits = load_limits(root / "data/config.yml")
    os.environ["TZ"] = limits.timezone
    if hasattr(time, "tzset"):
        time.tzset()
    _logging(root)
    router = Router(root)
    run_dir = root / "data/run"
    run_dir.mkdir(parents=True, exist_ok=True)
    socket_path = run_dir / "manager.sock"
    socket_path.unlink(missing_ok=True)
    unix = ThreadingUnixServer(str(socket_path), _handler(router, {}, True, Nonces()))
    os.chmod(socket_path, 0o600)
    keys = _keys()
    if not keys:
        raise ServiceError("必须配置 MANAGER_API_KEY")
    tcp = ThreadingHTTPServer(
        (settings.listen, settings.port), _handler(router, keys, False, Nonces()),
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(settings.certificate), str(settings.private_key))
    tcp.socket = context.wrap_socket(tcp.socket, server_side=True)
    stopped = threading.Event()
    threads = [
        threading.Thread(target=unix.serve_forever, name="unix-api", daemon=True),
        threading.Thread(target=_schedule_loop, args=(root, stopped), name="schedule", daemon=True),
        threading.Thread(target=_reconcile, args=(root,), name="reconcile", daemon=True),
        threading.Thread(target=tcp.serve_forever, name="tcp-api", daemon=True),
    ]
    for thread in threads: thread.start()
    LOGGER.info(
        "Manager API 已启动：unix=%s tcp=%s:%d", socket_path,
        settings.listen, settings.port,
    )
    try:
        while True: time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        stopped.set(); unix.shutdown(); unix.server_close(); socket_path.unlink(missing_ok=True)
        tcp.shutdown(); tcp.server_close()
        router.jobs.close()


def main() -> int:
    try:
        serve()
    except (ConfigError, ServiceError, OSError) as exc:
        print(f"错误：{exc}", file=os.sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
