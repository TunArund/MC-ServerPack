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

from .config import CONFIG_FILE, ConfigError, load, qq_credentials, secrets
from .monitor.power import PowerTarget, create_power
from .monitor.schedule import PowerSchedule, StateStore
from .monitor.service import MonitorService
from .qqbot.network import Network
from .qqbot.qqbot import QQBot, discover_user
from .qqbot.targets import Target, TargetPolicy


DATA_DIR = Path(os.environ.get("MCSP_DATA", "/app/data"))


class AsyncPower:
    """让同步 Provider 边界适配 QQ 事件循环。"""

    def __init__(self, power) -> None:
        self.power = power

    async def status(self):
        return await asyncio.to_thread(self.power.status)

    async def start(self):
        return await asyncio.to_thread(self.power.start)

    async def stop(self):
        return await asyncio.to_thread(self.power.stop)


async def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = load(CONFIG_FILE)
    power = create_power(
        config.power_api.provider,
        PowerTarget(config.power_api.instance_id, config.power_api.instance_region),
        secrets(),
        wait_after_action=False,
    )
    credentials = qq_credentials()
    bot = None
    if credentials:
        if not config.qqbot.targets:
            raise ConfigError("启用 QQ Bot 时至少需要一个 qqbot.targets")
        targets = {
            item.name: Target(item.kind, item.identifier)
            for item in config.qqbot.targets
        }
        bot = QQBot(
            Network(*credentials),
            TargetPolicy(
                tuple(targets.values()),
                tuple(targets[name].identifier for name in config.qqbot.admins),
                {
                    route.kind: frozenset(targets[name] for name in route.targets)
                    for route in config.qqbot.alerts
                },
            ),
            AsyncPower(power),
        )
    monitor = MonitorService(
        PowerSchedule(
            config.schedule.auto_start,
            config.schedule.start_at,
            config.schedule.auto_stop,
            config.schedule.stop_at,
        ),
        power,
        StateStore(DATA_DIR / "state.json"),
        heartbeat=DATA_DIR / "heartbeat",
        alert=bot.alert if bot else None,
    )
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


async def add_target(code: str | None = None) -> None:
    credentials = qq_credentials()
    if credentials is None:
        raise ConfigError("add-target 需要 QQBOT_SECRET_ID 与 QQBOT_SECRET_KEY")
    code = code or token_secrets.token_hex(2)
    if not re.fullmatch(r"[0-9a-f]{4}", code):
        raise ConfigError("验证码必须是四位小写十六进制字符")
    print(f"请向机器人发送：{code}", flush=True)
    try:
        user_openid = await discover_user(Network(*credentials), code)
    except asyncio.TimeoutError as exc:
        raise ConfigError("等待验证消息超时，请重新执行 add-target") from exc
    print(f"user_openid: {user_openid}")


def run() -> None:
    command = sys.argv[1:]
    if not command:
        asyncio.run(main())
    elif command[:1] == ["add-target"] and len(command) <= 2:
        asyncio.run(add_target(command[1] if len(command) == 2 else None))
    else:
        raise SystemExit("用法：python -m src.app [add-target [验证码]]")


if __name__ == "__main__":
    run()
