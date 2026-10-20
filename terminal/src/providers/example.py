"""自定义电源 Provider 示例；复制并改名为 <provider>.py。"""

from __future__ import annotations

import json
from urllib.request import Request, urlopen


URL = "https://example.com/mcsp/power"


def _request(
    action: str, instance_id: str, region: str,
    secret_id: str, secret_key: str,
) -> dict:
    del secret_id
    request = Request(
        URL,
        data=json.dumps({
            "action": action,
            "instance-id": instance_id,
            "instance-region": region,
        }).encode(),
        headers={
            "Authorization": f"Bearer {secret_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    with urlopen(request, timeout=60) as response:
        content = response.read()
    if not content:
        return {}
    reply = json.loads(content)
    if not isinstance(reply, dict):
        raise RuntimeError("转接口返回值必须是对象")
    return reply


def status(instance_id: str, region: str, secret_id: str, secret_key: str) -> dict:
    return _request("status", instance_id, region, secret_id, secret_key)


def start(instance_id: str, region: str, secret_id: str, secret_key: str) -> dict:
    return _request("start", instance_id, region, secret_id, secret_key)


def stop(instance_id: str, region: str, secret_id: str, secret_key: str) -> dict:
    return _request("stop", instance_id, region, secret_id, secret_key)
