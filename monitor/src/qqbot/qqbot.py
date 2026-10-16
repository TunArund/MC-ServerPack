"""QQ Bot command routing and power-operation confirmation."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter, OrderedDict
from collections.abc import Callable

from . import message
from .network import Network, Outgoing
from .targets import Incoming, TargetPolicy, incoming


COMMANDS = frozenset(("菜单", "开机", "关机", "确认关机"))
CONFIRM_SECONDS = 60
ALERT_WINDOW_SECONDS = 60
LOGGER = logging.getLogger(__name__)


def _button(identifier: str, command: str) -> dict:
    return {
        "id": identifier,
        "render_data": {
            "label": command,
            "visited_label": command,
            "style": 1,
        },
        "action": {
            "type": 2,
            "permission": {"type": 2},
            "data": command,
            "enter": True,
            "unsupport_tips": f"请直接发送「{command}」",
        },
    }


def menu_keyboard() -> dict:
    return {"content": {"rows": [{"buttons": [
        _button("power-start", "开机"),
        _button("power-stop", "关机"),
        _button("power-stop-confirm", "确认关机"),
    ]}]}}


def text(name: str, **values: object) -> Outgoing:
    return Outgoing(message.render(name, **values))


def menu(state: str) -> Outgoing:
    return Outgoing(message.render("menu", state=state), True, menu_keyboard())


class QQBot:
    def __init__(
        self, network: Network, policy: TargetPolicy, power,
        *, clock: Callable[[], float] = time.monotonic,
        alert_window: float = ALERT_WINDOW_SECONDS,
    ) -> None:
        self.network = network
        self.policy = policy
        self.power = power
        self.clock = clock
        self.alert_window = alert_window
        self.seen: OrderedDict[str, None] = OrderedDict()
        self.pending_stop: dict[tuple[str, str, str], float] = {}
        self.alerts: asyncio.Queue[str] = asyncio.Queue(maxsize=1000)

    async def run(self, stopped: asyncio.Event) -> None:
        network = asyncio.create_task(self.network.serve(self.handle, stopped))
        alerts = asyncio.create_task(self.serve_alerts())
        try:
            done, _ = await asyncio.wait(
                (network, alerts), return_when=asyncio.FIRST_COMPLETED,
            )
            task = next(iter(done))
            exception = task.exception()
            if exception:
                raise exception
            raise RuntimeError("QQ Bot 子任务意外停止")
        finally:
            network.cancel()
            alerts.cancel()
            await asyncio.gather(network, alerts, return_exceptions=True)

    async def alert(self, problem: str | None) -> None:
        """Queue a problem, or a recovery notification when problem is None."""
        content = problem if problem is not None else message.render("recovered")
        try:
            self.alerts.put_nowait(content)
        except asyncio.QueueFull:
            LOGGER.error("QQ 告警队列已满，丢弃告警：%s", content)

    async def serve_alerts(self) -> None:
        while True:
            await self.flush_alerts()

    async def flush_alerts(self) -> None:
        first = await self.alerts.get()
        if self.alert_window:
            await asyncio.sleep(self.alert_window)
        contents = [first]
        while not self.alerts.empty():
            contents.append(self.alerts.get_nowait())
        lines = []
        for content, count in Counter(contents).items():
            suffix = f"（{count} 次）" if count > 1 else ""
            lines.append(f"- {content}{suffix}")
        outgoing = Outgoing(message.render("alert", alerts="\n".join(lines)))
        for target in sorted(self.policy.targets, key=lambda item: (item.kind, item.identifier)):
            try:
                await self.network.push(target, outgoing)
            except Exception:
                LOGGER.exception("QQ 主动告警发送失败：%s/%s", target.kind, target.identifier)

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
            await self.network.reply(received, text("permission"))
            return
        if received.content == "开机":
            await self._operate(received, "开机", self.power.start)
        elif received.content == "关机":
            self.pending_stop[self._confirmation_key(received)] = self.clock() + CONFIRM_SECONDS
            await self.network.reply(received, text("confirm"))
        else:
            await self._confirm_stop(received)

    async def _menu(self, received: Incoming) -> None:
        try:
            status = await self.power.status()
            reply = menu(status.state)
        except Exception as exc:
            reply = menu(f"查询失败：{exc}")
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
            await self.network.reply(received, text("confirm-expired"))
            return
        await self._operate(received, "关机", self.power.stop)

    async def _operate(self, received: Incoming, action: str, operation) -> None:
        await self.network.reply(received, text("working", action=action))
        try:
            status = await operation()
            reply = text("done", action=action, state=status.state)
        except Exception as exc:
            reply = text("error", action=action, reason=exc)
        await self.network.reply(received, reply, sequence=2)
