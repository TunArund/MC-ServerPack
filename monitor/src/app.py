"""Monitor process entry point."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from pathlib import Path

from .config import CONFIG_FILE, ConfigError, load, qq_credentials, secrets
from .monitor.power import PowerTarget, create_power
from .monitor.schedule import PowerSchedule, StateStore
from .monitor.service import MonitorService
from .qqbot.network import Network
from .qqbot.qqbot import QQBot
from .qqbot.targets import Target, TargetPolicy


DATA_DIR = Path(os.environ.get("MCSP_DATA", "/app/data"))


class AsyncPower:
    """Adapt the synchronous provider boundary for the QQ event loop."""

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
        bot = QQBot(
            Network(*credentials),
            TargetPolicy(
                tuple(Target(item.kind, item.identifier) for item in config.qqbot.targets),
                config.qqbot.admins,
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


if __name__ == "__main__":
    asyncio.run(main())
