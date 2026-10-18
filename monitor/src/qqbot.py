"""QQ Bot 命令路由与电源操作确认。"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter, OrderedDict
from collections.abc import Callable
from pathlib import Path

import yaml

from .network import Incoming, Network, Outgoing, Target, incoming


COMMANDS = frozenset(("菜单", "开机", "关机", "确认关机"))
CONFIRM_SECONDS = 60
ALERT_WINDOW_SECONDS = 60
LOGGER = logging.getLogger(__name__)
TEMPLATE_FILE = Path(__file__).with_name("message.yml")


def _load_templates() -> dict[str, str]:
    try:
        value = yaml.safe_load(TEMPLATE_FILE.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise RuntimeError(f"QQ 消息模板读取失败：{exc}") from exc
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(template, str)
        for key, template in value.items()
    ):
        raise RuntimeError("QQ 消息模板必须是字符串对象")
    return value


TEMPLATES = _load_templates()


def render(name: str, **values: object) -> str:
    try:
        template = TEMPLATES[name]
    except KeyError as exc:
        raise RuntimeError(f"缺少 QQ 消息模板：{name}") from exc
    try:
        return template.format(**values)
    except KeyError as exc:
        raise RuntimeError(f"QQ 消息模板 {name} 缺少参数：{exc.args[0]}") from exc


class TargetPolicy:
    def __init__(
        self, targets: tuple[Target, ...], admins: tuple[str, ...],
        alert_routes: dict[str, frozenset[Target]] | None = None,
    ) -> None:
        self.targets = frozenset(targets)
        self.admins = frozenset(admins)
        self.alert_routes = alert_routes or {}

    def allows(self, received: Incoming) -> bool:
        return received.target in self.targets

    def is_admin(self, received: Incoming) -> bool:
        return received.sender_id in self.admins

    def alert_targets(self, kind: str) -> frozenset[Target]:
        return self.alert_routes.get(kind, frozenset())


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
    return Outgoing(render(name, **values))


def menu(state: str) -> Outgoing:
    return Outgoing(render("menu", state=state), True, menu_keyboard())


async def discover_user(network: Network, code: str, timeout: float = 300) -> str:
    """等待完全匹配的单聊验证码消息并返回 user_openid。"""
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    found: asyncio.Future[str] = loop.create_future()

    async def verify(event_name: str, event: dict) -> None:
        received = incoming(event_name, event)
        if (
            received is None
            or received.target.kind != "user"
            or received.content != code
            or found.done()
        ):
            return
        try:
            await network.reply(received, text("verified"))
        except Exception:
            LOGGER.exception("QQ 验证成功，但回复验证消息失败")
        finally:
            found.set_result(received.sender_id)
            stopped.set()

    server = asyncio.create_task(network.serve(verify, stopped))
    try:
        return await asyncio.wait_for(found, timeout=timeout)
    finally:
        stopped.set()
        server.cancel()
        await asyncio.gather(server, return_exceptions=True)


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
        self.alerts: asyncio.Queue[tuple[str, str]] = asyncio.Queue(maxsize=1000)

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

    async def alert(self, kind: str, content: str) -> None:
        """将分类告警加入其配置目标的发送队列。"""
        if kind == "recovered":
            content = render("recovered")
        try:
            self.alerts.put_nowait((kind, content))
        except asyncio.QueueFull:
            LOGGER.error("QQ 告警队列已满，丢弃 %s 告警：%s", kind, content)

    async def serve_alerts(self) -> None:
        while True:
            await self.flush_alerts()

    async def flush_alerts(self) -> None:
        first = await self.alerts.get()
        if self.alert_window:
            await asyncio.sleep(self.alert_window)
        alerts = [first]
        while not self.alerts.empty():
            alerts.append(self.alerts.get_nowait())
        targets = set().union(*(
            self.policy.alert_targets(kind) for kind, _ in alerts
        ))
        for target in sorted(targets, key=lambda item: (item.kind, item.identifier)):
            contents = [
                content for kind, content in alerts
                if target in self.policy.alert_targets(kind)
            ]
            lines = []
            for content, count in Counter(contents).items():
                suffix = f"（{count} 次）" if count > 1 else ""
                lines.append(f"- {content}{suffix}")
            outgoing = Outgoing(render("alert", alerts="\n".join(lines)))
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
