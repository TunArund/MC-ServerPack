"""Manager cloud-instance power operations."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from .tencent import ApiCall, TencentApiError


class PowerError(RuntimeError):
    pass


@dataclass(frozen=True)
class PowerTarget:
    instance_id: str


@dataclass(frozen=True)
class InstanceStatus:
    state: str
    public_ip: str = ""


class TencentPower:
    def __init__(
        self, target: PowerTarget, api_call: ApiCall, *,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.target = target
        self.api_call = api_call
        self.sleeper = sleeper

    def api(self, action: str, payload: dict) -> dict:
        try:
            return self.api_call("cvm", action, payload)
        except (PowerError, TencentApiError):
            raise
        except Exception as exc:
            raise PowerError(f"cvm.{action} 失败：{exc}") from exc

    def status(self) -> InstanceStatus:
        reply = self.api("DescribeInstances", {"InstanceIds": [self.target.instance_id]})
        instances = reply.get("InstanceSet") or []
        if len(instances) != 1 or instances[0].get("InstanceId") != self.target.instance_id:
            raise PowerError("DescribeInstances 未返回唯一目标实例")
        state = instances[0].get("InstanceState")
        if not isinstance(state, str) or not state:
            raise PowerError("DescribeInstances 缺少实例状态")
        return InstanceStatus(state, (instances[0].get("PublicIpAddresses") or [""])[0])

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
            self.api("StartInstances", {"InstanceIds": [self.target.instance_id]})
        elif current.state not in ("RUNNING", "STARTING"):
            raise PowerError(f"实例处于 {current.state}，不能开机")
        return self.wait("RUNNING", 180)

    def stop(self, current: InstanceStatus | None = None) -> InstanceStatus:
        current = current or self.status()
        if current.state == "STOPPED":
            return current
        if current.state == "RUNNING":
            self.api("StopInstances", {
                "InstanceIds": [self.target.instance_id],
                "StopType": "SOFT", "StoppedMode": "STOP_CHARGING",
            })
        elif current.state != "STOPPING":
            raise PowerError(f"实例处于 {current.state}，不能关机")
        return self.wait("STOPPED", 360)
