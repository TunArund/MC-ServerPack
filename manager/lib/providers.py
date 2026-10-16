"""Build Manager power providers from validated configuration and secrets."""

from __future__ import annotations

import os
from pathlib import Path

from .config import MANAGER_ROOT, ManagerLimits
from .power import Power, PowerError, PowerTarget, webhook_adapter
from .tencent import tencent_adapter


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


def create_power(settings: ManagerLimits, manager_root: Path = MANAGER_ROOT) -> Power:
    if (
        not settings.poweroff_enabled
        or not settings.power_instance_id
        or not settings.power_region
    ):
        raise PowerError("云关机未启用或配置不完整")
    secrets = load_env(manager_root / ".env")
    target = PowerTarget(settings.power_instance_id, settings.power_region)
    secret_id = os.environ.get("POWER_SECRET_ID", secrets.get("POWER_SECRET_ID", "")).strip()
    secret_key = os.environ.get("POWER_SECRET_KEY", secrets.get("POWER_SECRET_KEY", "")).strip()
    if settings.power_provider == "tencent":
        return Power(tencent_adapter(
            target, secret_id, secret_key, manager_root=manager_root,
        ), wait_after_action=False)
    if settings.power_provider == "webhook":
        return Power(
            webhook_adapter(target, settings.power_url or "", secret_key),
            wait_after_action=False,
        )
    raise PowerError(f"不支持的 power-api provider：{settings.power_provider}")
