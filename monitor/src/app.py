"""Monitor 进程入口。"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets as token_secrets
import signal
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import (
    CONFIG_FILE, ConfigError, load, manager_api_secret, qq_credentials,
    secrets,
)
from .manager_sdk import ManagerSdk
from .message import messages
from .network import Network
from .observability_sdk import configure_logging as configure_observability
from .power_sdk import PowerTarget, create_power
from .qqbot import QQBot, discover_target
from .schedule import MonitorService, PowerSchedule, StateStore
from .targets import TargetStore


DATA_DIR = Path(os.environ.get("MCSP_DATA", "/app/data"))


def configure_logging(timezone) -> None:
    configure_observability(
        {"": DATA_DIR / "service.log"}, stream=True, timezone=timezone,
    )


class AsyncPower:
    """让同步 Provider 边界适配 QQ 事件循环。"""

    def __init__(self, power, record_manual_power=None, before_stop=None) -> None:
        self.power = power
        self.record_manual_power = record_manual_power
        self.before_stop = before_stop

    async def status(self):
        return await asyncio.to_thread(self.power.status)

    async def start(self):
        result = await asyncio.to_thread(self.power.start)
        if self.record_manual_power is not None:
            await self.record_manual_power("start")
        return result

    async def stop(self):
        if self.before_stop is not None:
            await self.before_stop()
        result = await asyncio.to_thread(self.power.stop)
        if self.record_manual_power is not None:
            await self.record_manual_power("stop")
        return result


async def main() -> None:
    config = load(CONFIG_FILE)
    timezone = ZoneInfo(config.timezone)
    configure_logging(timezone)
    power = create_power(
        config.power_api.provider,
        PowerTarget(config.power_api.instance_id, config.power_api.instance_region),
        secrets(),
        wait_after_action=False,
    )
    ca = DATA_DIR / "tls/ca.crt"
    manager_api = ManagerSdk(
        config.manager_api.host, config.manager_api.port,
        manager_api_secret(), identity="monitor",
        ca=ca if ca.is_file() else None, job_timeout=900,
    )
    monitor = MonitorService(
        PowerSchedule(
            config.schedule.auto_start,
            config.schedule.start_at,
            config.schedule.auto_stop,
            config.schedule.stop_at,
            config.timezone,
        ),
        power,
        StateStore(DATA_DIR / "state.json"),
        heartbeat=DATA_DIR / "heartbeat",
        stop_packs=manager_api.stop_all,
    )
    credentials = qq_credentials()
    bot = None
    if credentials:
        bot = QQBot(
            Network(*credentials),
            AsyncPower(
                power, monitor.record_manual_power,
                before_stop=monitor.stop_all_packs,
            ),
            cancel_reservation=monitor.cancel_next_start,
            restore_reservation=monitor.restore_next_start,
            snooze_alerts=monitor.snooze_alerts,
            targets=TargetStore(DATA_DIR / "qqbot.json"),
            manager_status=manager_api,
        )
        monitor.alert = bot.alert
        monitor.notify = bot.notify
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(name, stopped.set)
    logging.getLogger(__name__).info("Monitor 已启动")
    tasks = [asyncio.create_task(monitor.run(stopped), name="monitor")]
    if bot:
        tasks.append(asyncio.create_task(bot.run(stopped), name="qqbot"))
        logging.getLogger(__name__).info("QQ Bot 已启用")
    stop_waiter = asyncio.create_task(stopped.wait(), name="shutdown")
    try:
        done, _ = await asyncio.wait(
            [*tasks, stop_waiter], return_when=asyncio.FIRST_COMPLETED,
        )
        if stop_waiter not in done:
            task = next(iter(done))
            exception = task.exception()
            if exception:
                raise exception
            raise RuntimeError(f"{task.get_name()} 意外停止")
    finally:
        stopped.set()
        stop_waiter.cancel()
        for task in tasks:
            task.cancel()
        await asyncio.gather(stop_waiter, *tasks, return_exceptions=True)


async def add_admin(code: str | None = None) -> None:
    credentials = qq_credentials()
    if credentials is None:
        raise ConfigError("add-admin 需要 QQBOT_APP_ID 与 QQBOT_APP_KEY")
    code = code or token_secrets.token_hex(2)
    if not re.fullmatch(r"[0-9a-f]{4}", code):
        raise ConfigError("验证码必须是四位小写十六进制字符")
    print(f"请私聊机器人发送：{code}", flush=True)
    try:
        received = await discover_target(Network(*credentials), code, "user")
    except asyncio.TimeoutError as exc:
        raise ConfigError("等待验证消息超时，请重新执行 add-admin") from exc
    TargetStore(DATA_DIR / "qqbot.json").add_admin(
        received.sender_id, received.union_openid,
    )
    print(f"admin_openid: {received.sender_id}")


async def test_alert() -> None:
    credentials = qq_credentials()
    if credentials is None:
        raise ConfigError("test-alert 需要 QQBOT_APP_ID 与 QQBOT_APP_KEY")
    targets = TargetStore(DATA_DIR / "qqbot.json").private_targets()
    if not targets:
        raise ConfigError("尚未添加管理员；请先运行 ./terminal/mcsp.sh monitor add-admin")
    network = Network(*credentials)
    message = messages.render("alert-test")
    for target in sorted(targets, key=lambda item: (item.kind, item.identifier)):
        await network.send(target, message)
    print(f"测试告警已发送：{len(targets)} 个单聊目标")


def reset_targets() -> None:
    TargetStore.reset_file(DATA_DIR / "qqbot.json")
    print("已重置 Monitor QQ 管理员与订阅目标")


def run() -> None:
    command = sys.argv[1:]
    if not command:
        asyncio.run(main())
    elif command[:1] == ["add-admin"] and len(command) <= 2:
        asyncio.run(add_admin(command[1] if len(command) == 2 else None))
    elif command == ["test-alert"]:
        asyncio.run(test_alert())
    elif command == ["reset-targets"]:
        reset_targets()
    else:
        raise SystemExit(
            "用法：python -m src.app [add-admin [验证码]|test-alert|reset-targets]"
        )


if __name__ == "__main__":
    run()
