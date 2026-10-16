"""Minimal Tencent Cloud SDK adapter shared by domain services."""

from __future__ import annotations

import json
from collections.abc import Callable


class TencentApiError(RuntimeError):
    pass


ApiCall = Callable[[str, str, dict], dict]


def create_api(region: str, secret_id: str, secret_key: str) -> ApiCall:
    if not secret_id or not secret_key:
        raise TencentApiError("缺少 POWER_SECRET_ID/POWER_SECRET_KEY")
    try:
        from tencentcloud.common import credential
        from tencentcloud.cvm.v20170312 import cvm_client, models as cvm_models
        from tencentcloud.tat.v20201028 import tat_client, models as tat_models
    except ImportError as exc:
        raise TencentApiError("缺少腾讯云 SDK；请安装 requirements.txt") from exc

    auth = credential.Credential(secret_id, secret_key)
    clients = {
        "cvm": (cvm_client.CvmClient(auth, region), cvm_models),
        "tat": (tat_client.TatClient(auth, region), tat_models),
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
