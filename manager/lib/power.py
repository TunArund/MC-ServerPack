"""与云厂商无关的实例电源状态机。"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from types import ModuleType
from typing import Optional

from .provider import ProviderError, load_provider


class PowerError(RuntimeError):
    """电源操作失败。"""


@dataclass(frozen=True)
class PowerTarget:
    instance_id: str
    instance_region: str = ""


@dataclass(frozen=True)
class InstanceStatus:
    state: str
    public_ip: str = ""


class Power:
    """持有密钥与公共状态转换，具体 API 调用交给 Provider。"""

    def __init__(
        self, adapter: ModuleType, target: PowerTarget, secret_id: str,
        secret_key: str, *,
        sleeper: Callable[[float], None] = time.sleep,
        wait_after_action: bool = True,
    ) -> None:
        self.adapter = adapter
        self.target = target
        self.secret_id = secret_id
        self.secret_key = secret_key
        self.sleeper = sleeper
        self.wait_after_action = wait_after_action

    def api(self, action: str) -> dict:
        try:
            reply = getattr(self.adapter, action)(
                self.target.instance_id, self.target.instance_region,
                self.secret_id, self.secret_key,
            )
        except PowerError:
            raise
        except Exception as exc:
            raise PowerError(f"power-api {action} 失败：{exc}") from exc
        if not isinstance(reply, dict):
            raise PowerError(f"power-api {action} 返回值必须是对象")
        return reply

    def status(self) -> InstanceStatus:
        reply = self.api("status")
        state = reply.get("state")
        if not isinstance(state, str) or not state:
            raise PowerError("power-api status 缺少 state")
        public_ip = reply.get("public-ip", "")
        if not isinstance(public_ip, str):
            raise PowerError("power-api status 的 public-ip 必须是字符串")
        return InstanceStatus(state.upper(), public_ip)

    def wait(self, expected: str, seconds: int) -> InstanceStatus:
        deadline = time.monotonic() + seconds
        while True:
            current = self.status()
            if current.state == expected:
                return current
            if time.monotonic() >= deadline:
                raise PowerError(f"等待 {expected} 超时；当前为 {current.state}")
            self.sleeper(min(5, max(0, deadline - time.monotonic())))

    def start(self, current: Optional[InstanceStatus] = None) -> InstanceStatus:
        current = current or self.status()
        if current.state == "STOPPED":
            self.api("start")
            if not self.wait_after_action:
                return InstanceStatus("STARTING", current.public_ip)
        elif current.state not in ("RUNNING", "STARTING"):
            raise PowerError(f"实例处于 {current.state}，不能开机")
        return self.wait("RUNNING", 180)

    def stop(self, current: Optional[InstanceStatus] = None) -> InstanceStatus:
        current = current or self.status()
        if current.state == "STOPPED":
            return current
        if current.state == "RUNNING":
            self.api("stop")
            if not self.wait_after_action:
                return InstanceStatus("STOPPING", current.public_ip)
        elif current.state != "STOPPING":
            raise PowerError(f"实例处于 {current.state}，不能关机")
        return self.wait("STOPPED", 360)


def create_power(
    provider: str, target: PowerTarget, secrets: dict[str, str],
    *, wait_after_action: bool = True,
) -> Power:
    try:
        adapter = load_provider(provider)
    except ProviderError as exc:
        raise PowerError(str(exc)) from exc
    return Power(
        adapter, target,
        secrets.get("POWER_SECRET_ID", ""),
        secrets.get("POWER_SECRET_KEY", ""),
        wait_after_action=wait_after_action,
    )
