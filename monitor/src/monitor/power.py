"""Provider-neutral cloud-instance power state machine."""

from __future__ import annotations

import importlib.util
import inspect
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType


PROVIDERS_DIR = Path(__file__).resolve().parent / "providers"
PROVIDER_ID_RE = re.compile(r"[A-Za-z0-9-]+")


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


def load_adapter(provider: str, *, providers_dir: Path = PROVIDERS_DIR) -> ModuleType:
    if not PROVIDER_ID_RE.fullmatch(provider):
        raise PowerError("无效 provider ID；仅允许大小写字母、数字和减号")
    source = providers_dir / f"{provider}_api.py"
    if not source.is_file():
        raise PowerError(f"未找到 provider：{source.name}")
    module_name = f"{__package__}.providers._loaded_{provider.replace('-', '_')}_api"
    spec = importlib.util.spec_from_file_location(module_name, source)
    if spec is None or spec.loader is None:
        raise PowerError(f"provider 无法加载：{source.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        sys.modules.pop(module_name, None)
        raise PowerError(f"provider 加载失败：{source.name}：{exc}") from exc
    for action in ("status", "start", "stop"):
        function = getattr(module, action, None)
        if not callable(function):
            raise PowerError(f"provider {source.name} 未提供 {action}")
        try:
            signature = inspect.signature(function)
            signature.bind("instance", "region", "id", "key")
        except (TypeError, ValueError) as exc:
            raise PowerError(f"provider {source.name} 的 {action} 接口不正确") from exc
        if tuple(signature.parameters) != ("instance_id", "region", "secret_id", "secret_key"):
            raise PowerError(f"provider {source.name} 的 {action} 参数名不正确")
    return module


class Power:
    """Own credentials and shared state transitions; delegate API calls only."""

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

    def start(self) -> InstanceStatus:
        current = self.status()
        if current.state == "STOPPED":
            self.api("start")
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
    return Power(
        load_adapter(provider), target,
        secrets.get("POWER_SECRET_ID", ""),
        secrets.get("POWER_SECRET_KEY", ""),
        wait_after_action=wait_after_action,
    )
