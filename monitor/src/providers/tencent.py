"""腾讯云 CVM 电源 Provider。"""

from __future__ import annotations

from collections.abc import Callable


ApiCall = Callable[[str, dict], dict]


def _create_api(region: str, secret_id: str, secret_key: str) -> ApiCall:
    if not secret_id or not secret_key:
        raise RuntimeError("缺少 POWER_SECRET_ID/POWER_SECRET_KEY")
    try:
        from tencentcloud.common import credential
        from tencentcloud.common.common_client import CommonClient
        from tencentcloud.common.profile.client_profile import ClientProfile
        from tencentcloud.common.profile.http_profile import HttpProfile
    except ImportError as exc:
        raise RuntimeError("缺少腾讯云 SDK；请安装 requirements.txt") from exc
    auth = credential.Credential(secret_id, secret_key)
    http_profile = HttpProfile(endpoint="cvm.tencentcloudapi.com")
    client = CommonClient(
        "cvm", "2017-03-12", auth, region,
        ClientProfile(httpProfile=http_profile),
    )

    def call(action: str, payload: dict) -> dict:
        try:
            response = client.call_json(action, payload)
            return response.get("Response", response)
        except Exception as exc:
            raise RuntimeError(f"cvm.{action} 失败：{exc}") from exc

    return call


def _request(
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
    return _request("status", instance_id, region, secret_id, secret_key)


def start(instance_id: str, region: str, secret_id: str, secret_key: str) -> dict:
    return _request("start", instance_id, region, secret_id, secret_key)


def stop(instance_id: str, region: str, secret_id: str, secret_key: str) -> dict:
    return _request("stop", instance_id, region, secret_id, secret_key)
