"""QQ Bot 命令路由、通知订阅与电源操作确认。"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import Counter, OrderedDict
from collections.abc import Awaitable, Callable

from .message import MessageError, messages
from .network import Incoming, Network, Outgoing, Target, incoming, target_label
from .targets import SubscriptionRequest, TargetStateError, TargetStore


SNOOZE_SERVICES = {
    f"alerts-snooze-{hours}h": hours for hours in (1, 2, 5)
}
SERVICES = frozenset((
    "menu", "power-start", "power-stop", "power-stop-confirm",
    "schedule-cancel", "schedule-restore", "request-approve", "request-reject",
    *SNOOZE_SERVICES,
))
DECISION_SERVICES = frozenset(("request-approve", "request-reject"))
REQUEST_RE = re.compile(r"订阅(?:\s+(.{1,32}))?$")
DECISION_RE = re.compile(r"(批准|拒绝)\s+([0-9a-f]{6})$")
CONFIRM_SECONDS = 60
ALERT_WINDOW_SECONDS = 60
LOGGER = logging.getLogger(__name__)


def alert_message(kind: str, contents: list[str]) -> Outgoing:
    lines = []
    for content, count in Counter(contents).items():
        suffix = f"（{count} 次）" if count > 1 else ""
        lines.append(f"- {content}{suffix}")
    return messages.render(f"alert-{kind}", alerts="\n".join(lines))


async def discover_target(
    network: Network, code: str, kind: str, timeout: float = 300,
) -> Incoming:
    """等待指定场景中完全匹配的验证码消息。"""
    if kind not in ("user", "group"):
        raise ValueError("目标类型只能是 user 或 group")
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    found: asyncio.Future[Incoming] = loop.create_future()

    async def verify(event_name: str, event: dict) -> None:
        received = incoming(event_name, event)
        if (
            received is None
            or received.target.kind != kind
            or received.content != code
            or found.done()
        ):
            return
        try:
            await network.reply(received, messages.render("verified"))
        except Exception:
            LOGGER.exception("QQ 验证成功，但回复验证消息失败")
        finally:
            found.set_result(received)
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
        self, network: Network, power, *,
        clock: Callable[[], float] = time.monotonic,
        alert_window: float = ALERT_WINDOW_SECONDS,
        cancel_reservation: Callable[[], Awaitable[object]] | None = None,
        restore_reservation: Callable[[], Awaitable[object]] | None = None,
        snooze_alerts: Callable[[int], Awaitable[object]] | None = None,
        targets: TargetStore | None = None,
        manager_status=None,
    ) -> None:
        try:
            messages.set_services(SERVICES)
        except MessageError as exc:
            raise RuntimeError(str(exc)) from exc
        self.network = network
        self.power = power
        self.clock = clock
        self.alert_window = alert_window
        self.cancel_reservation = cancel_reservation
        self.restore_reservation = restore_reservation
        self.snooze_alerts = snooze_alerts
        self.targets = targets or TargetStore()
        self.manager_status = manager_status
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
        """将分类告警加入订阅目标发送队列。"""
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
        targets = self.targets.private_targets()
        if not targets:
            return
        kinds = list(dict.fromkeys(kind for kind, _ in alerts))
        for target in sorted(targets, key=lambda item: (item.kind, item.identifier)):
            for kind in kinds:
                contents = [
                    content for item_kind, content in alerts if item_kind == kind
                ]
                # State transitions sampled during the aggregation window are
                # history, not concurrent faults. Only report the latest state.
                if kind.startswith("state-mismatch-"):
                    contents = contents[-1:]
                try:
                    await self.network.push(target, alert_message(kind, contents))
                except Exception:
                    LOGGER.exception(
                        "QQ 主动告警发送失败：%s/%s",
                        target.kind, target.identifier,
                    )

    async def notify(self, state: str, public_ip: str = "") -> None:
        """向群聊发送公开的稳定电源状态通知。"""
        if state not in ("running", "stopped"):
            raise ValueError(f"未知公开状态：{state}")
        targets = self.targets.group_targets()
        if not targets:
            return
        if state == "running":
            if self.manager_status is None:
                raise RuntimeError("Manager 状态接口未配置")
            modpacks = await asyncio.to_thread(self.manager_status.list, public_ip)
            running = [
                item for item in modpacks
                if item.state in ("running", "healthy")
            ]
            if not running:
                raise RuntimeError("Manager 尚无运行中的整合包")
            servers = "\n\n".join(
                f"整合包：{item.name}\n地址：{item.address}"
                for item in running
            )
            outgoing = messages.render("public-started", servers=servers)
        else:
            outgoing = messages.render("public-stopped")
        for target in sorted(targets, key=lambda item: item.identifier):
            try:
                await self.network.push(target, outgoing)
            except Exception:
                LOGGER.exception("QQ 公开状态通知发送失败：%s", target_label(target))

    async def handle(self, event_name: str, event: dict) -> None:
        received = incoming(event_name, event)
        if received is None:
            return
        if received.message_id in self.seen:
            return
        self.seen[received.message_id] = None
        if len(self.seen) > 500:
            self.seen.popitem(last=False)

        LOGGER.info("收到 QQ 消息：target=%s", target_label(received.target))

        decision = DECISION_RE.fullmatch(received.content)
        if decision and received.target.kind == "user":
            LOGGER.info("识别 QQ 服务：request-decision")
            await self._decide(received, decision.group(1), decision.group(2))
            return

        request = REQUEST_RE.fullmatch(received.content)
        if request:
            LOGGER.info("识别 QQ 服务：subscription-request")
            await self._request(received, request.group(1))
            return

        is_admin = self.targets.is_admin(
            received.sender_id, received.union_openid,
            scene=received.target.kind,
        )
        if not (
            is_admin
            or self.targets.is_target(
                received.target.kind, received.target.identifier,
            )
        ):
            LOGGER.info("忽略未授权 QQ 消息：target=%s", target_label(received.target))
            return

        service = messages.action(received.content)
        if service is None:
            LOGGER.info("忽略未识别 QQ 命令：target=%s", target_label(received.target))
            return
        LOGGER.info("识别 QQ 服务：%s target=%s", service, target_label(received.target))
        if service == "menu":
            await self._menu(received, read_only=not is_admin)
            return
        if service in DECISION_SERVICES:
            if not is_admin:
                await self.network.reply(received, messages.render("permission"))
            else:
                await self.network.reply(received, messages.render("decision-usage"))
            return
        if not is_admin:
            await self.network.reply(received, messages.render("permission"))
            return
        if service == "power-start":
            await self._operate(received, "开机", self.power.start)
        elif service == "power-stop":
            key = self._confirmation_key(received)
            if self.pending_stop.get(key, 0) >= self.clock():
                await self.network.reply(received, messages.render("confirm-pending"))
            else:
                self.pending_stop[key] = self.clock() + CONFIRM_SECONDS
                await self.network.reply(received, messages.render("confirm-stop"))
        elif service == "power-stop-confirm":
            await self._confirm_stop(received)
        elif service == "schedule-cancel":
            await self._cancel_reservation(received)
        elif service == "schedule-restore":
            await self._restore_reservation(received)
        elif service in SNOOZE_SERVICES:
            await self._snooze(received, SNOOZE_SERVICES[service])
        else:
            LOGGER.warning("QQ 消息配置引用了未注册服务：%s", service)

    async def _request(self, received: Incoming, nickname: str | None) -> None:
        kind = received.target.kind
        identifier = received.target.identifier
        provided = (nickname or "").strip()
        if kind == "group":
            name_label = "群备注" if provided else "群标识"
            name = provided or f"…{identifier[-6:]}"
        else:
            name_label = "昵称"
            name = provided or received.sender_name or f"用户 {identifier[-6:]}"
        if not self.targets.admins:
            await self.network.reply(received, messages.render("request-no-admin"))
            return
        if self.targets.is_target(kind, identifier) or (
            kind == "user"
            and self.targets.is_admin(
                received.sender_id, received.union_openid, scene="user",
            )
        ):
            await self.network.reply(received, messages.render("request-exists"))
            return
        requester = received.sender_name or received.sender_id[-6:]
        request, notify = self.targets.request(
            kind, identifier, name, requester,
            requester_id=received.sender_id,
            message_id=received.message_id,
            union_openid=received.union_openid,
        )
        if request is None:
            await self.network.reply(received, messages.render("request-exists"))
            return
        if not notify:
            await self.network.reply(received, messages.render("request-pending"))
            return
        await self.network.reply(received, messages.render("request-submitted"))
        outgoing = messages.render(
            "subscription-request",
            kind_label="群聊" if kind == "group" else "单聊",
            name_label=name_label,
            name=request.name,
            requester=request.requester,
            request_id=request.request_id,
            button_data={
                "request-approve": f"批准 {request.request_id}",
                "request-reject": f"拒绝 {request.request_id}",
            },
        )
        for admin in sorted(self.targets.admins):
            try:
                await self.network.push(Target("user", admin), outgoing)
            except Exception:
                LOGGER.exception("订阅申请发送管理员失败")

    async def _decide(
        self, received: Incoming, action: str, request_id: str,
    ) -> None:
        approved_by = self.targets.admin_id(
            received.sender_id, received.union_openid, scene="user",
        )
        if approved_by is None:
            await self.network.reply(received, messages.render("permission"))
            return
        approved = action == "批准"
        try:
            request = self.targets.decide(
                request_id, approved, approved_by=approved_by,
            )
        except TargetStateError as exc:
            await self.network.reply(
                received, messages.render("request-error", reason=exc),
            )
            return
        result = "request-approved-admin" if approved else "request-rejected-admin"
        await self.network.reply(received, messages.render(result, name=request.name))
        LOGGER.info(
            "订阅申请已处理：request=%s result=%s target=%s",
            request.request_id, "approved" if approved else "rejected",
            target_label(Target(request.kind, request.identifier)),
        )
        await self._notify_requester(
            request,
            messages.render("request-approved" if approved else "request-rejected"),
        )

    async def _notify_requester(
        self, request: SubscriptionRequest, message: Outgoing,
    ) -> None:
        target = Target(request.kind, request.identifier)
        try:
            await self.network.reply_to(target, request.message_id, message)
            return
        except Exception:
            LOGGER.exception("被动回复订阅申请结果失败，改用主动消息")
        try:
            await self.network.push(target, message)
        except Exception:
            LOGGER.exception("主动通知订阅申请结果失败")

    async def _menu(self, received: Incoming, *, read_only: bool) -> None:
        try:
            status = await self.power.status()
            state = status.state
        except Exception as exc:
            state = f"查询失败：{exc}"
        template = "menu-readonly" if read_only else "menu"
        await self.network.reply(received, messages.render(template, state=state))

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
            await self.network.reply(received, messages.render("confirm-expired"))
            return
        await self._operate(received, "关机", self.power.stop)

    async def _cancel_reservation(self, received: Incoming) -> None:
        if self.cancel_reservation is None:
            await self.network.reply(
                received, messages.render("error", action="取消预订", reason="功能未配置"),
            )
            return
        try:
            event = await self.cancel_reservation()
            reply = messages.render(
                "cancelled", due=event.due_at.strftime("%m-%d %H:%M"),
            )
        except Exception as exc:
            reply = messages.render("error", action="取消预订", reason=exc)
        await self.network.reply(received, reply)

    async def _restore_reservation(self, received: Incoming) -> None:
        if self.restore_reservation is None:
            await self.network.reply(
                received, messages.render("error", action="恢复预订", reason="功能未配置"),
            )
            return
        try:
            event = await self.restore_reservation()
            reply = messages.render(
                "restored", due=event.due_at.strftime("%m-%d %H:%M"),
            )
        except Exception as exc:
            reply = messages.render("error", action="恢复预订", reason=exc)
        await self.network.reply(received, reply)

    async def _snooze(self, received: Incoming, hours: int) -> None:
        if self.snooze_alerts is None:
            await self.network.reply(
                received, messages.render("error", action="静默告警", reason="功能未配置"),
            )
            return
        try:
            until = await self.snooze_alerts(hours)
            reply = messages.render("snoozed", until=until.strftime("%m-%d %H:%M"))
        except Exception as exc:
            reply = messages.render("error", action="静默告警", reason=exc)
        await self.network.reply(received, reply)

    async def _operate(self, received: Incoming, action: str, operation) -> None:
        await self.network.reply(received, messages.render("working", action=action))
        try:
            status = await operation()
            reply = messages.render("done", action=action, state=status.state)
            LOGGER.info("QQ 电源操作已提交：action=%s state=%s", action, status.state)
        except Exception as exc:
            reply = messages.render("error", action=action, reason=exc)
            LOGGER.exception("QQ 电源操作失败：action=%s", action)
        await self.network.reply(received, reply, sequence=2)
