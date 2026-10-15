"""Tencent Cloud power operations initiated by Terminal."""

from __future__ import annotations

import base64
import json
import shlex
import time
from collections.abc import Callable
from dataclasses import dataclass


class CloudError(RuntimeError):
    pass


@dataclass(frozen=True)
class CloudTarget:
    region: str
    instance_id: str
    manager_dir: str
    manager_user: str


@dataclass(frozen=True)
class InstanceStatus:
    state: str
    public_ip: str = ""


ApiCall = Callable[[str, str, dict], dict]


class TencentCloud:
    def __init__(
        self, target: CloudTarget, secret_id: str, secret_key: str, *,
        api_call: ApiCall | None = None, sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.target = target
        self.sleeper = sleeper
        self.api_call = api_call or self._sdk(secret_id, secret_key)

    def _sdk(self, secret_id: str, secret_key: str) -> ApiCall:
        if not secret_id or not secret_key:
            raise CloudError("缺少 POWER_SECRET_ID/POWER_SECRET_KEY")
        try:
            from tencentcloud.common import credential
            from tencentcloud.cvm.v20170312 import cvm_client, models as cvm_models
            from tencentcloud.tat.v20201028 import tat_client, models as tat_models
        except ImportError as exc:
            raise CloudError("缺少腾讯云 SDK；请安装 requirements.txt") from exc
        auth = credential.Credential(secret_id, secret_key)
        clients = {
            "cvm": (cvm_client.CvmClient(auth, self.target.region), cvm_models),
            "tat": (tat_client.TatClient(auth, self.target.region), tat_models),
        }

        def call(service: str, action: str, payload: dict) -> dict:
            try:
                client, models = clients[service]
                request = getattr(models, action + "Request")()
                request.from_json_string(json.dumps(payload))
                response = getattr(client, action)(request)
                return json.loads(response.to_json_string())
            except Exception as exc:
                raise CloudError(f"{service}.{action} 失败：{exc}") from exc

        return call

    def api(self, service: str, action: str, payload: dict) -> dict:
        try:
            return self.api_call(service, action, payload)
        except CloudError:
            raise
        except Exception as exc:
            raise CloudError(f"{service}.{action} 失败：{exc}") from exc

    def status(self) -> InstanceStatus:
        reply = self.api("cvm", "DescribeInstances", {"InstanceIds": [self.target.instance_id]})
        instances = reply.get("InstanceSet") or []
        if len(instances) != 1 or instances[0].get("InstanceId") != self.target.instance_id:
            raise CloudError("DescribeInstances 未返回唯一目标实例")
        state = instances[0].get("InstanceState")
        if not isinstance(state, str) or not state:
            raise CloudError("DescribeInstances 缺少实例状态")
        return InstanceStatus(state, (instances[0].get("PublicIpAddresses") or [""])[0])

    def wait(self, expected: str, seconds: int) -> InstanceStatus:
        deadline = time.monotonic() + seconds
        while True:
            current = self.status()
            if current.state == expected:
                return current
            if time.monotonic() >= deadline:
                raise CloudError(f"等待 {expected} 超时；当前为 {current.state}")
            self.sleeper(min(5, max(0, deadline - time.monotonic())))

    def start(self) -> InstanceStatus:
        current = self.status()
        if current.state == "STOPPED":
            self.api("cvm", "StartInstances", {"InstanceIds": [self.target.instance_id]})
        elif current.state not in ("RUNNING", "STARTING"):
            raise CloudError(f"实例处于 {current.state}，不能开机")
        return self.wait("RUNNING", 180)

    def stop_modpacks(self, timeout: int = 240) -> None:
        command = f"cd {shlex.quote(self.target.manager_dir)} && ./manager.sh stop-all"
        reply = self.api("tat", "RunCommand", {
            "InstanceIds": [self.target.instance_id],
            "Content": base64.b64encode(command.encode()).decode(),
            "CommandType": "SHELL", "Username": self.target.manager_user,
            "Timeout": timeout,
        })
        invocation = reply.get("InvocationId")
        if not invocation:
            raise CloudError("TAT RunCommand 未返回 InvocationId")
        deadline = time.monotonic() + timeout + 30
        while True:
            reply = self.api("tat", "DescribeInvocationTasks", {
                "Filters": [{"Name": "invocation-id", "Values": [invocation]}],
            })
            tasks = [item for item in reply.get("InvocationTaskSet") or []
                     if item.get("InstanceId") == self.target.instance_id]
            if len(tasks) > 1:
                raise CloudError("TAT 返回多个目标任务")
            if tasks:
                task = tasks[0]
                state = task.get("TaskStatus")
                if state == "SUCCESS":
                    if (task.get("TaskResult") or {}).get("ExitCode") == 0:
                        return
                    raise CloudError("TAT stop-all 退出码异常")
                if state in {"FAILED", "TIMEOUT", "TASK_TIMEOUT", "DELIVER_FAILED", "CANCELLED"}:
                    raise CloudError(f"TAT stop-all 失败：{state}")
            if time.monotonic() >= deadline:
                raise CloudError("TAT stop-all 查询超时")
            self.sleeper(min(5, max(0, deadline - time.monotonic())))

    def stop(self, *, continue_on_stop_failure: bool = False) -> InstanceStatus:
        current = self.status()
        if current.state == "STOPPED":
            return current
        if current.state == "RUNNING":
            try:
                self.stop_modpacks()
            except CloudError:
                if not continue_on_stop_failure:
                    raise
            self.api("cvm", "StopInstances", {
                "InstanceIds": [self.target.instance_id],
                "StopType": "SOFT", "StoppedMode": "STOP_CHARGING",
            })
        elif current.state != "STOPPING":
            raise CloudError(f"实例处于 {current.state}，不能关机")
        return self.wait("STOPPED", 360)
