"""Monitor process entry point."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from pathlib import Path

from .config import CONFIG_FILE, load, secrets
from .monitor import MonitorService
from .power import PowerTarget, create_power
from .state import StateStore


DATA_DIR = Path(os.environ.get("MCSP_DATA", "/app/data"))


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
    service = MonitorService(
        config,
        power,
        StateStore(DATA_DIR / "state.json"),
        heartbeat=DATA_DIR / "heartbeat",
    )
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(name, stopped.set)
    logging.getLogger(__name__).info("Monitor 已启动")
    await service.run(stopped)


if __name__ == "__main__":
    asyncio.run(main())
