"""Tencent Cloud tccli adapter for the Manager runtime."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from .power import InstanceStatus, PowerCall, PowerError, PowerTarget


Runner = Callable[..., subprocess.CompletedProcess[str]]


def tencent_adapter(
    target: PowerTarget, secret_id: str, secret_key: str, *,
    manager_root: Path, runner: Runner = subprocess.run,
    executable: str | None = None,
) -> PowerCall:
    if not secret_id or not secret_key:
        raise PowerError("缺少 POWER_SECRET_ID/POWER_SECRET_KEY")
    bundled = manager_root / ".venv/bin/tccli"
    command_path = executable or (str(bundled) if bundled.is_file() else shutil.which("tccli"))
    if not command_path:
        raise PowerError("缺少 tccli；请安装 manager/requirements.txt")
    environment = os.environ.copy()
    environment.update({
        "TENCENTCLOUD_SECRET_ID": secret_id,
        "TENCENTCLOUD_SECRET_KEY": secret_key,
    })

    def api(action: str, *arguments: str) -> dict:
        command = [
            command_path, "cvm", action, *arguments,
            "--region", target.instance_region, "--output", "json",
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

    def call(action: str) -> InstanceStatus | None:
        actions = {
            "status": ("DescribeInstances", ("--InstanceIds", json.dumps([target.instance_id]))),
            "start": ("StartInstances", ("--InstanceIds", json.dumps([target.instance_id]))),
            "stop": ("StopInstances", (
                "--InstanceIds", json.dumps([target.instance_id]),
                "--StopType", "SOFT", "--StoppedMode", "STOP_CHARGING",
            )),
        }
        api_action, arguments = actions[action]
        reply = api(api_action, *arguments)
        if action != "status":
            return None
        instances = reply.get("InstanceSet") or []
        if len(instances) != 1 or instances[0].get("InstanceId") != target.instance_id:
            raise PowerError("DescribeInstances 未返回唯一目标实例")
        state = instances[0].get("InstanceState")
        if not isinstance(state, str) or not state:
            raise PowerError("DescribeInstances 缺少实例状态")
        return InstanceStatus(state, (instances[0].get("PublicIpAddresses") or [""])[0])

    return call
