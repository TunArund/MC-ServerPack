"""QQ access-token, HTTP reply and WebSocket transport."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .targets import Incoming, Target


TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
API_URL = "https://api.sgroup.qq.com"
INTENTS = 1 << 25
LOGGER = logging.getLogger(__name__)


class NetworkError(RuntimeError):
    pass


@dataclass(frozen=True)
class Outgoing:
    content: str
    markdown: bool = False
    keyboard: dict | None = None


class Network:
    def __init__(self, app_id: str, app_key: str) -> None:
        if not app_id or not app_key:
            raise ValueError("QQ AppID/AppSecret 不能为空")
        self.app_id = app_id
        self.app_key = app_key
        self._token = ""
        self._expires_at = 0.0
        self._token_lock = asyncio.Lock()
        self.ready = asyncio.Event()

    @staticmethod
    def _request(
        method: str, url: str, body: dict | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict:
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
        request = urllib.request.Request(
            url, data=data, method=method,
            headers={"Accept": "application/json", "Content-Type": "application/json", **(headers or {})},
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                content = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[-500:]
            raise NetworkError(f"QQ HTTP {exc.code}：{detail}") from exc
        except OSError as exc:
            raise NetworkError(f"QQ 网络请求失败：{exc}") from exc
        if not content:
            return {}
        try:
            reply = json.loads(content)
        except ValueError as exc:
            raise NetworkError("QQ API 返回了无效 JSON") from exc
        if not isinstance(reply, dict):
            raise NetworkError("QQ API 返回值必须是对象")
        return reply

    async def token(self) -> str:
        if self._token and self._expires_at > time.time() + 120:
            return self._token
        async with self._token_lock:
            if self._token and self._expires_at > time.time() + 120:
                return self._token
            reply = await asyncio.to_thread(
                self._request,
                "POST",
                TOKEN_URL,
                {"appId": self.app_id, "clientSecret": self.app_key},
            )
            token = reply.get("access_token")
            if not isinstance(token, str) or not token:
                raise NetworkError("QQ access_token 获取失败")
            try:
                expires = int(reply.get("expires_in") or 3600)
            except (TypeError, ValueError) as exc:
                raise NetworkError("QQ access_token 有效期无效") from exc
            self._token = token
            self._expires_at = time.time() + expires
            return token

    @staticmethod
    def _body(
        message: Outgoing, *, message_id: str | None = None, sequence: int = 1,
    ) -> dict:
        body: dict = {
            "msg_type": 2 if message.markdown else 0,
        }
        if message_id:
            body.update(msg_id=message_id, msg_seq=sequence)
        if message.markdown:
            body.update(content=" ", markdown={"content": message.content})
        else:
            body["content"] = message.content
        if message.keyboard:
            body["keyboard"] = message.keyboard
        return body

    async def _send(
        self, target: Target, message: Outgoing, *,
        message_id: str | None = None, sequence: int = 1,
    ) -> None:
        token = await self.token()
        await asyncio.to_thread(
            self._request,
            "POST",
            self._endpoint(target),
            self._body(message, message_id=message_id, sequence=sequence),
            {"Authorization": f"QQBot {token}"},
        )

    @staticmethod
    def _endpoint(target: Target) -> str:
        path = "users" if target.kind == "user" else "groups"
        return f"{API_URL}/v2/{path}/{target.identifier}/messages"

    async def reply(self, incoming: Incoming, message: Outgoing, *, sequence: int = 1) -> None:
        await self._send(
            incoming.target, message,
            message_id=incoming.message_id, sequence=sequence,
        )

    async def push(self, target: Target, message: Outgoing) -> None:
        try:
            await asyncio.wait_for(self.ready.wait(), timeout=30)
        except asyncio.TimeoutError as exc:
            raise NetworkError("QQ WebSocket 尚未就绪，不能主动告警") from exc
        await self._send(target, message)

    async def gateway(self) -> str:
        token = await self.token()
        reply = await asyncio.to_thread(
            self._request,
            "GET",
            f"{API_URL}/gateway",
            None,
            {"Authorization": f"QQBot {token}"},
        )
        url = reply.get("url")
        if not isinstance(url, str) or not url:
            raise NetworkError("QQ gateway 地址缺失")
        return url

    async def serve(
        self,
        handler: Callable[[str, dict], Awaitable[None]],
        stopped: asyncio.Event,
    ) -> None:
        import websockets

        backoff = 5
        while not stopped.is_set():
            try:
                token = await self.token()
                url = await self.gateway()
                headers = {"Authorization": f"QQBot {token}"}
                header_name = (
                    "additional_headers"
                    if "additional_headers" in inspect.signature(websockets.connect).parameters
                    else "extra_headers"
                )
                async with websockets.connect(
                    url, max_size=2**22, open_timeout=20,
                    **{header_name: headers},
                ) as socket:
                    backoff = 5
                    self.ready.clear()
                    sequence: int | None = None
                    heartbeat: asyncio.Task | None = None

                    async def beat(interval: int) -> None:
                        while True:
                            await asyncio.sleep(interval / 1000)
                            await socket.send(json.dumps({"op": 1, "d": sequence}))

                    try:
                        async for raw in socket:
                            event = json.loads(raw)
                            if isinstance(event.get("s"), int):
                                sequence = event["s"]
                            operation = event.get("op")
                            if operation == 0 and event.get("t") in ("READY", "RESUMED"):
                                self.ready.set()
                            if operation == 10:
                                if heartbeat:
                                    heartbeat.cancel()
                                interval = int((event.get("d") or {}).get("heartbeat_interval", 40000))
                                heartbeat = asyncio.create_task(beat(interval))
                                await socket.send(json.dumps({"op": 2, "d": {
                                    "token": f"QQBot {token}",
                                    "intents": INTENTS,
                                    "shard": [0, 1],
                                }}))
                            elif operation == 0 and event.get("t") in (
                                "GROUP_AT_MESSAGE_CREATE", "C2C_MESSAGE_CREATE",
                            ):
                                try:
                                    await handler(event["t"], event.get("d") or {})
                                except Exception:
                                    LOGGER.exception("QQ 消息处理失败")
                            elif operation in (7, 9):
                                break
                    finally:
                        self.ready.clear()
                        if heartbeat:
                            heartbeat.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOGGER.warning("QQ 连接失败：%s", exc)
            if not stopped.is_set():
                try:
                    await asyncio.wait_for(stopped.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    backoff = min(backoff * 2, 300)
