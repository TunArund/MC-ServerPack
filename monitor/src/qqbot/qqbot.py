"""QQ Bot command routing and power-operation confirmation."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Callable

from . import message
from .network import Network
from .targets import Incoming, TargetPolicy, incoming


COMMANDS = frozenset(("菜单", "开机", "关机", "确认关机"))
CONFIRM_SECONDS = 60


class QQBot:
    def __init__(
        self, network: Network, policy: TargetPolicy, power,
        *, clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.network = network
        self.policy = policy
        self.power = power
        self.clock = clock
        self.seen: OrderedDict[str, None] = OrderedDict()
        self.pending_stop: dict[tuple[str, str, str], float] = {}

    async def run(self, stopped: asyncio.Event) -> None:
        await self.network.serve(self.handle, stopped)

    async def handle(self, event_name: str, event: dict) -> None:
        received = incoming(event_name, event)
        if received is None or not self.policy.allows(received):
            return
        if received.message_id in self.seen:
            return
        self.seen[received.message_id] = None
        if len(self.seen) > 500:
            self.seen.popitem(last=False)
        if received.content not in COMMANDS:
            return
        if received.content == "菜单":
            await self._menu(received)
            return
        if not self.policy.is_admin(received):
            await self.network.reply(received, message.text("permission"))
            return
        if received.content == "开机":
            await self._operate(received, "开机", self.power.start)
        elif received.content == "关机":
            self.pending_stop[self._confirmation_key(received)] = self.clock() + CONFIRM_SECONDS
            await self.network.reply(received, message.text("confirm"))
        else:
            await self._confirm_stop(received)

    async def _menu(self, received: Incoming) -> None:
        try:
            status = await self.power.status()
            reply = message.menu(status.state)
        except Exception as exc:
            reply = message.menu(f"查询失败：{exc}")
        await self.network.reply(received, reply)

    @staticmethod
    def _confirmation_key(received: Incoming) -> tuple[str, str, str]:
        return (
            received.target.kind,
            received.target.identifier,
            received.sender_id,
        )

    async def _confirm_stop(self, received: Incoming) -> None:
        key = self._confirmation_key(received)
        expires = self.pending_stop.pop(key, 0)
        if expires < self.clock():
            await self.network.reply(received, message.text("confirm-expired"))
            return
        await self._operate(received, "关机", self.power.stop)

    async def _operate(self, received: Incoming, action: str, operation) -> None:
        await self.network.reply(received, message.text("working", action=action))
        try:
            status = await operation()
            reply = message.text("done", action=action, state=status.state)
        except Exception as exc:
            reply = message.text("error", action=action, reason=exc)
        await self.network.reply(received, reply, sequence=2)
