"""Minimal Tencent Cloud SDK adapter shared by domain services."""

from __future__ import annotations

import json
from collections.abc import Callable

from .power import InstanceStatus, PowerCall, PowerError, PowerTarget


class TencentApiError(RuntimeError):
    pass


ApiCall = Callable[[str, str, dict], dict]


def create_api(region: str, secret_id: str, secret_key: str) -> ApiCall:
    if not secret_id or not secret_key:
        raise TencentApiError("缺少 POWER_SECRET_ID/POWER_SECRET_KEY")
    try:
        from tencentcloud.common import credential
        from tencentcloud.cvm.v20170312 import cvm_client, models as cvm_models
    except ImportError as exc:
        raise TencentApiError("缺少腾讯云 SDK；请安装 requirements.txt") from exc

    auth = credential.Credential(secret_id, secret_key)
    clients = {
        "cvm": (cvm_client.CvmClient(auth, region), cvm_models),
    }

    def call(service: str, action: str, payload: dict) -> dict:
        try:
            client, models = clients[service]
            request = getattr(models, action + "Request")()
            request.from_json_string(json.dumps(payload))
            response = getattr(client, action)(request)
            return json.loads(response.to_json_string())
        except Exception as exc:
            raise TencentApiError(f"{service}.{action} 失败：{exc}") from exc

    return call


def tencent_adapter(
    target: PowerTarget, secret_id: str, secret_key: str, *,
    api_call: ApiCall | None = None,
) -> PowerCall:
    api = api_call or create_api(target.instance_region, secret_id, secret_key)

    def call(action: str) -> InstanceStatus | None:
        actions = {
            "status": ("DescribeInstances", {"InstanceIds": [target.instance_id]}),
            "start": ("StartInstances", {"InstanceIds": [target.instance_id]}),
            "stop": ("StopInstances", {
                "InstanceIds": [target.instance_id],
                "StopType": "SOFT", "StoppedMode": "STOP_CHARGING",
            }),
        }
        api_action, payload = actions[action]
        try:
            reply = api("cvm", api_action, payload)
        except (PowerError, TencentApiError):
            raise
        except Exception as exc:
            raise PowerError(f"cvm.{api_action} 失败：{exc}") from exc
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
