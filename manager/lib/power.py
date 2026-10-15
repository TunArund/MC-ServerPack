"""Idempotent Tencent Cloud power-off operation for the Manager host."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from .config import MANAGER_ROOT, ManagerLimits


class PowerError(RuntimeError):
    pass


Runner = Callable[..., subprocess.CompletedProcess[str]]


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


class TencentPower:
    def __init__(
        self, settings: ManagerLimits, manager_root: Path = MANAGER_ROOT,
        runner: Runner = subprocess.run, executable: str | None = None,
    ) -> None:
        if not settings.poweroff_enabled or not settings.power_region or not settings.power_instance_id:
            raise PowerError("云关机未启用或配置不完整")
        self.region = settings.power_region
        self.instance_id = settings.power_instance_id
        self.manager_root = manager_root
        self.runner = runner
        bundled = manager_root / ".venv/bin/tccli"
        self.executable = executable or (str(bundled) if bundled.is_file() else shutil.which("tccli"))
        if not self.executable:
            raise PowerError("缺少 tccli；请安装 manager/requirements.txt")
        secrets = load_env(manager_root / ".env")
        secret_id = os.environ.get("POWER_SECRET_ID", secrets.get("POWER_SECRET_ID", "")).strip()
        secret_key = os.environ.get("POWER_SECRET_KEY", secrets.get("POWER_SECRET_KEY", "")).strip()
        if not secret_id or not secret_key:
            raise PowerError("缺少 POWER_SECRET_ID/POWER_SECRET_KEY")
        self.environment = os.environ.copy()
        self.environment.update({
            "TENCENTCLOUD_SECRET_ID": secret_id,
            "TENCENTCLOUD_SECRET_KEY": secret_key,
        })

    def api(self, action: str, *arguments: str) -> dict:
        command = [
            self.executable, "cvm", action, *arguments,
            "--region", self.region, "--output", "json",
        ]
        try:
            result = self.runner(
                command, cwd=self.manager_root, env=self.environment, text=True,
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

    def state(self) -> str:
        response = self.api("DescribeInstances", "--InstanceIds", json.dumps([self.instance_id]))
        instances = response.get("InstanceSet") or []
        if len(instances) != 1 or instances[0].get("InstanceId") != self.instance_id:
            raise PowerError("DescribeInstances 未返回唯一目标实例")
        state = instances[0].get("InstanceState")
        if not isinstance(state, str) or not state:
            raise PowerError("DescribeInstances 缺少实例状态")
        return state

    def stop(self) -> None:
        state = self.state()
        if state in ("STOPPED", "STOPPING"):
            return
        if state != "RUNNING":
            raise PowerError(f"实例处于 {state}，不能提交关机")
        self.api(
            "StopInstances", "--InstanceIds", json.dumps([self.instance_id]),
            "--StopType", "SOFT", "--StoppedMode", "STOP_CHARGING",
        )
