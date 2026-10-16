"""Provider-neutral cloud-instance power state machine and API adapters."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class PowerError(RuntimeError):
    pass


@dataclass(frozen=True)
class PowerTarget:
    instance_id: str
    instance_region: str = ""


@dataclass(frozen=True)
class InstanceStatus:
    state: str
    public_ip: str = ""


PowerCall = Callable[[str], InstanceStatus | None]
WebhookCall = Callable[[str, dict], dict]


class Power:
    """Shared start/stop/status behavior independent of the cloud API."""

    def __init__(
        self, api_call: PowerCall, *, sleeper: Callable[[float], None] = time.sleep,
        wait_after_action: bool = True,
    ) -> None:
        self.api_call = api_call
        self.sleeper = sleeper
        self.wait_after_action = wait_after_action

    def status(self) -> InstanceStatus:
        result = self.api_call("status")
        if not isinstance(result, InstanceStatus):
            raise PowerError("电源 API status 未返回实例状态")
        return result

    def wait(self, expected: str, seconds: int) -> InstanceStatus:
        deadline = time.monotonic() + seconds
        while True:
            current = self.status()
            if current.state == expected:
                return current
            if time.monotonic() >= deadline:
                raise PowerError(f"等待 {expected} 超时；当前为 {current.state}")
            self.sleeper(min(5, max(0, deadline - time.monotonic())))

    def start(self) -> InstanceStatus:
        current = self.status()
        if current.state == "STOPPED":
            self.api_call("start")
            if not self.wait_after_action:
                return InstanceStatus("STARTING", current.public_ip)
        elif current.state not in ("RUNNING", "STARTING"):
            raise PowerError(f"实例处于 {current.state}，不能开机")
        return self.wait("RUNNING", 180)

    def stop(self, current: InstanceStatus | None = None) -> InstanceStatus:
        current = current or self.status()
        if current.state == "STOPPED":
            return current
        if current.state == "RUNNING":
            self.api_call("stop")
            if not self.wait_after_action:
                return InstanceStatus("STOPPING", current.public_ip)
        elif current.state != "STOPPING":
            raise PowerError(f"实例处于 {current.state}，不能关机")
        return self.wait("STOPPED", 360)


def webhook_adapter(
    target: PowerTarget, url: str, secret_key: str, *,
    api_call: WebhookCall | None = None,
) -> PowerCall:
    if not url:
        raise PowerError("power-api.url 未配置")
    if not secret_key:
        raise PowerError("Webhook 需要 POWER_SECRET_KEY")

    def http(action: str, payload: dict) -> dict:
        request = Request(
            url,
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {secret_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=60) as response:
                content = response.read()
        except HTTPError as exc:
            detail = exc.read().decode(errors="replace").strip()[-500:]
            raise PowerError(f"power-api {action} HTTP {exc.code}：{detail}") from exc
        except (OSError, URLError) as exc:
            raise PowerError(f"power-api {action} 请求失败：{exc}") from exc
        if not content:
            return {}
        try:
            result = json.loads(content)
        except (TypeError, ValueError) as exc:
            raise PowerError(f"power-api {action} 返回无效 JSON") from exc
        if not isinstance(result, dict):
            raise PowerError(f"power-api {action} 返回值必须是对象")
        return result

    raw_call = api_call or http

    def call(action: str) -> InstanceStatus | None:
        payload = {
            "action": action,
            "instance-id": target.instance_id,
            "instance-region": target.instance_region,
        }
        try:
            reply = raw_call(action, payload)
        except PowerError:
            raise
        except Exception as exc:
            raise PowerError(f"power-api {action} 失败：{exc}") from exc
        if action != "status":
            return None
        state = reply.get("state")
        if not isinstance(state, str) or not state:
            raise PowerError("power-api status 缺少 state")
        public_ip = reply.get("public-ip", "")
        if not isinstance(public_ip, str):
            raise PowerError("power-api status 的 public-ip 必须是字符串")
        return InstanceStatus(state.upper(), public_ip)

    return call
