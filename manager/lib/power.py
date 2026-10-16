"""Provider-neutral power-off state machine and Manager-side API adapters."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import MANAGER_ROOT, ManagerLimits


class PowerError(RuntimeError):
    pass


Runner = Callable[..., subprocess.CompletedProcess[str]]
PowerCall = Callable[[str], str | None]
WebhookCall = Callable[[str, dict], dict]


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return values
    except OSError as exc:
        raise PowerError(f"无法读取 {path}：{exc}") from exc
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("\"'")
    return values


class Power:
    """Shared idempotent power-off behavior independent of the provider."""

    def __init__(self, api_call: PowerCall) -> None:
        self.api_call = api_call

    def state(self) -> str:
        state = self.api_call("status")
        if not isinstance(state, str) or not state:
            raise PowerError("电源 API status 未返回实例状态")
        return state.upper()

    def stop(self) -> None:
        state = self.state()
        if state in ("STOPPED", "STOPPING"):
            return
        if state != "RUNNING":
            raise PowerError(f"实例处于 {state}，不能提交关机")
        self.api_call("stop")


def tencent_adapter(
    settings: ManagerLimits, manager_root: Path = MANAGER_ROOT,
    runner: Runner = subprocess.run, executable: str | None = None,
) -> PowerCall:
    if not settings.poweroff_enabled or not settings.power_region or not settings.power_instance_id:
        raise PowerError("云关机未启用或配置不完整")
    bundled = manager_root / ".venv/bin/tccli"
    command_path = executable or (str(bundled) if bundled.is_file() else shutil.which("tccli"))
    if not command_path:
        raise PowerError("缺少 tccli；请安装 manager/requirements.txt")
    values = load_env(manager_root / ".env")
    secret_id = os.environ.get("POWER_SECRET_ID", values.get("POWER_SECRET_ID", "")).strip()
    secret_key = os.environ.get("POWER_SECRET_KEY", values.get("POWER_SECRET_KEY", "")).strip()
    if not secret_id or not secret_key:
        raise PowerError("缺少 POWER_SECRET_ID/POWER_SECRET_KEY")
    environment = os.environ.copy()
    environment.update({
        "TENCENTCLOUD_SECRET_ID": secret_id,
        "TENCENTCLOUD_SECRET_KEY": secret_key,
    })

    def api(action: str, *arguments: str) -> dict:
        command = [
            command_path, "cvm", action, *arguments,
            "--region", settings.power_region, "--output", "json",
        ]
        try:
            result = runner(
                command, cwd=manager_root, env=environment, text=True,
                capture_output=True, timeout=60, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PowerError(f"{action} 无法执行：{exc}") from exc
        if result.returncode:
            raise PowerError(f"{action} 失败：{result.stderr.strip()[-500:]}")
        try:
            response = json.loads(result.stdout)["Response"]
        except (KeyError, TypeError, ValueError) as exc:
            raise PowerError(f"{action} 返回无效 JSON") from exc
        if response.get("Error"):
            raise PowerError(f"{action} 返回错误：{response['Error']}")
        return response

    def call(action: str) -> str | None:
        if action == "status":
            response = api(
                "DescribeInstances", "--InstanceIds",
                json.dumps([settings.power_instance_id]),
            )
            instances = response.get("InstanceSet") or []
            if len(instances) != 1 or instances[0].get("InstanceId") != settings.power_instance_id:
                raise PowerError("DescribeInstances 未返回唯一目标实例")
            state = instances[0].get("InstanceState")
            if not isinstance(state, str) or not state:
                raise PowerError("DescribeInstances 缺少实例状态")
            return state
        api(
            "StopInstances", "--InstanceIds", json.dumps([settings.power_instance_id]),
            "--StopType", "SOFT", "--StoppedMode", "STOP_CHARGING",
        )
        return None

    return call


def webhook_adapter(
    settings: ManagerLimits, manager_root: Path = MANAGER_ROOT, *,
    api_call: WebhookCall | None = None,
) -> PowerCall:
    if (
        not settings.poweroff_enabled
        or settings.power_provider != "webhook"
        or not settings.power_url
        or not settings.power_region
        or not settings.power_instance_id
    ):
        raise PowerError("Webhook 云关机未启用或配置不完整")
    values = load_env(manager_root / ".env")
    secret_key = os.environ.get("POWER_SECRET_KEY", values.get("POWER_SECRET_KEY", "")).strip()
    if not secret_key:
        raise PowerError("Webhook 需要 POWER_SECRET_KEY")

    def http(action: str, payload: dict) -> dict:
        request = Request(
            settings.power_url,
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
            reply = json.loads(content)
        except (TypeError, ValueError) as exc:
            raise PowerError(f"power-api {action} 返回无效 JSON") from exc
        if not isinstance(reply, dict):
            raise PowerError(f"power-api {action} 返回值必须是对象")
        return reply

    raw_call = api_call or http

    def call(action: str) -> str | None:
        payload = {
            "action": action,
            "instance-id": settings.power_instance_id,
            "instance-region": settings.power_region,
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
        return state

    return call


def create_power(settings: ManagerLimits, manager_root: Path = MANAGER_ROOT) -> Power:
    if settings.power_provider == "tencent":
        return Power(tencent_adapter(settings, manager_root))
    if settings.power_provider == "webhook":
        return Power(webhook_adapter(settings, manager_root))
    raise PowerError(f"不支持的 power-api provider：{settings.power_provider}")
