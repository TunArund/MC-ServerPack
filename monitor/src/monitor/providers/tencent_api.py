"""Self-contained Tencent Cloud power provider."""

from __future__ import annotations

import json
from collections.abc import Callable


ApiCall = Callable[[str, dict], dict]


def _create_api(region: str, secret_id: str, secret_key: str) -> ApiCall:
    if not secret_id or not secret_key:
        raise RuntimeError("缺少 POWER_SECRET_ID/POWER_SECRET_KEY")
    try:
        from tencentcloud.common import credential
        from tencentcloud.cvm.v20170312 import cvm_client, models as cvm_models
    except ImportError as exc:
        raise RuntimeError("缺少腾讯云 SDK；请安装 requirements.txt") from exc
    auth = credential.Credential(secret_id, secret_key)
    client = cvm_client.CvmClient(auth, region)

    def call(action: str, payload: dict) -> dict:
        try:
            request = getattr(cvm_models, action + "Request")()
            request.from_json_string(json.dumps(payload))
            response = getattr(client, action)(request)
            return json.loads(response.to_json_string())
        except Exception as exc:
            raise RuntimeError(f"cvm.{action} 失败：{exc}") from exc

    return call


def _invoke(
    action: str, instance_id: str, region: str,
    secret_id: str, secret_key: str,
) -> dict:
    api = _create_api(region, secret_id, secret_key)
    actions = {
        "status": ("DescribeInstances", {"InstanceIds": [instance_id]}),
        "start": ("StartInstances", {"InstanceIds": [instance_id]}),
        "stop": ("StopInstances", {
            "InstanceIds": [instance_id], "StopType": "SOFT",
            "StoppedMode": "STOP_CHARGING",
        }),
    }
    api_action, payload = actions[action]
    reply = api(api_action, payload)
    if action != "status":
        return {}
    instances = reply.get("InstanceSet") or []
    if len(instances) != 1 or instances[0].get("InstanceId") != instance_id:
        raise RuntimeError("DescribeInstances 未返回唯一目标实例")
    state = instances[0].get("InstanceState")
    if not isinstance(state, str) or not state:
        raise RuntimeError("DescribeInstances 缺少实例状态")
    return {
        "state": state,
        "public-ip": (instances[0].get("PublicIpAddresses") or [""])[0],
    }


def status(instance_id: str, region: str, secret_id: str, secret_key: str) -> dict:
    return _invoke("status", instance_id, region, secret_id, secret_key)


def start(instance_id: str, region: str, secret_id: str, secret_key: str) -> dict:
    return _invoke("start", instance_id, region, secret_id, secret_key)


def stop(instance_id: str, region: str, secret_id: str, secret_key: str) -> dict:
    return _invoke("stop", instance_id, region, secret_id, secret_key)
