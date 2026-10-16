"""Build Terminal power providers from validated configuration and secrets."""

from __future__ import annotations

from .config import PowerApiConfig
from .power import Power, PowerError, PowerTarget, webhook_adapter
from .tencent import tencent_adapter


def create_power(settings: PowerApiConfig, secrets: dict[str, str]) -> Power:
    target = PowerTarget(settings.instance_id, settings.instance_region)
    secret_id = secrets.get("POWER_SECRET_ID", "")
    secret_key = secrets.get("POWER_SECRET_KEY", "")
    if settings.provider == "tencent":
        return Power(tencent_adapter(target, secret_id, secret_key))
    if settings.provider == "webhook":
        return Power(webhook_adapter(target, settings.url or "", secret_key))
    raise PowerError(f"不支持的 power-api provider：{settings.provider}")
