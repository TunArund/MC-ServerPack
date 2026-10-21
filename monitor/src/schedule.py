"""编排电源日程、持久化事件状态并执行 Monitor 循环。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .power_sdk import InstanceStatus, PowerError

ZONE = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class PowerSchedule:
    auto_start: bool
    start_at: str | None
    auto_stop: bool
    stop_at: str | None
    timezone: str = "Asia/Shanghai"

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


@dataclass(frozen=True)
class PowerEvent:
    key: str
    action: str
    due_at: datetime


def _at(local: datetime, clock: str) -> datetime:
    hour, minute = (int(part) for part in clock.split(":"))
    return local.replace(hour=hour, minute=minute, second=0, microsecond=0)


def latest_event(now: datetime, schedule: PowerSchedule) -> PowerEvent | None:
    """返回最近启用的事件，并包含昨日未处理事件以便补执行。"""
    local = now.astimezone(schedule.zone)
    candidates: list[PowerEvent] = []
    definitions = (
        (schedule.auto_start, schedule.start_at, "start"),
        (schedule.auto_stop, schedule.stop_at, "stop"),
    )
    for enabled, clock, action in definitions:
        if not enabled or clock is None:
            continue
        today = _at(local, clock)
        due = today if today <= local else today - timedelta(days=1)
        candidates.append(PowerEvent(f"{due.date().isoformat()}:{action}", action, due))
    return max(candidates, key=lambda event: event.due_at) if candidates else None


def next_start_event(now: datetime, schedule: PowerSchedule) -> PowerEvent | None:
    """返回下一次自动开机事件，不将已过的今日时间视为待执行。"""
    if not schedule.auto_start or schedule.start_at is None:
        return None
    local = now.astimezone(schedule.zone)
    due = _at(local, schedule.start_at)
    if due <= local:
        due += timedelta(days=1)
    return PowerEvent(f"{due.date().isoformat()}:start", "start", due)


class StateError(RuntimeError):
    pass


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.completed: set[str] = set()
        self.cancelled: set[str] = set()
        self.snooze_until: datetime | None = None
        self.manual_action: str | None = None
        self.manual_at: datetime | None = None
        self.power_state: str | None = None
        self.notified_power_state: str | None = None
        self.initialized = False
        self._load()

    def _load(self) -> None:
        try:
            content = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError as exc:
            raise StateError(f"调度状态无法读取：{exc}") from exc
        try:
            value = json.loads(content)
        except ValueError as exc:
            raise StateError(f"调度状态格式无效：{exc}") from exc
        if isinstance(value, list):
            if not all(isinstance(item, str) for item in value):
                raise StateError("旧调度状态必须是字符串数组")
            self.completed = set(value)
            self.initialized = True
            return
        if not isinstance(value, dict):
            raise StateError("调度状态必须是对象")
        completed = value.get("completed", [])
        cancelled = value.get("cancelled", [])
        if not all(
            isinstance(items, list) and all(isinstance(item, str) for item in items)
            for items in (completed, cancelled)
        ):
            raise StateError("调度事件状态必须是字符串数组")
        initialized = value.get("initialized", False)
        if not isinstance(initialized, bool):
            raise StateError("initialized 必须是布尔值")
        snooze = value.get("alert-snooze-until")
        if snooze is not None:
            if not isinstance(snooze, str):
                raise StateError("alert-snooze-until 必须是时间字符串")
            try:
                parsed = datetime.fromisoformat(snooze)
            except ValueError as exc:
                raise StateError("alert-snooze-until 时间无效") from exc
            if parsed.tzinfo is None:
                raise StateError("alert-snooze-until 必须包含时区")
            self.snooze_until = parsed
        manual = value.get("last-manual-power")
        if manual is not None:
            if not isinstance(manual, dict) or manual.get("action") not in ("start", "stop"):
                raise StateError("last-manual-power 格式无效")
            try:
                manual_at = datetime.fromisoformat(str(manual["at"]))
            except (KeyError, ValueError) as exc:
                raise StateError("last-manual-power.at 时间无效") from exc
            if manual_at.tzinfo is None:
                raise StateError("last-manual-power.at 必须包含时区")
            self.manual_action = manual["action"]
            self.manual_at = manual_at
        power_state = value.get("power-state")
        if power_state is not None and power_state not in ("RUNNING", "STOPPED"):
            raise StateError("power-state 只能是 RUNNING、STOPPED 或 null")
        self.power_state = power_state
        notified = value.get("notified-power-state", power_state)
        if notified is not None and notified not in ("RUNNING", "STOPPED"):
            raise StateError("notified-power-state 只能是 RUNNING、STOPPED 或 null")
        self.notified_power_state = notified
        self.completed = set(completed)
        self.cancelled = set(cancelled)
        self.initialized = initialized

    def contains(self, key: str) -> bool:
        return key in self.completed or key in self.cancelled

    def is_cancelled(self, key: str) -> bool:
        return key in self.cancelled

    def initialize(self, event: PowerEvent | None, today: date) -> None:
        if self.initialized:
            return
        if event is not None:
            self.completed.add(event.key)
        self.initialized = True
        self._save(today)

    def mark(self, key: str, today: date) -> None:
        self.completed.add(key)
        self._save(today)

    def cancel(self, key: str, today: date) -> None:
        self.cancelled.add(key)
        self._save(today)

    def restore(self, key: str, today: date) -> None:
        if key not in self.cancelled:
            raise StateError("下一次自动开机没有被取消")
        self.cancelled.remove(key)
        self._save(today)

    def update_power_state(self, state: str, today: date) -> str | None:
        """记录稳定电源状态；首次观察不产生公开通知。"""
        if state not in ("RUNNING", "STOPPED"):
            return None
        if self.power_state is None:
            self.power_state = state
            self.notified_power_state = state
            self._save(today)
            return None
        if self.power_state != state:
            self.power_state = state
            self._save(today)
        return state if self.notified_power_state != state else None

    def ack_power_notification(self, state: str, today: date) -> None:
        if state not in ("RUNNING", "STOPPED") or state != self.power_state:
            raise StateError("不能确认无效的电源状态通知")
        self.notified_power_state = state
        self._save(today)

    def record_manual_power(self, action: str, now: datetime) -> None:
        if action not in ("start", "stop"):
            raise StateError("人工电源操作只能是 start 或 stop")
        local = now
        self.manual_action = action
        self.manual_at = local
        self._save(local.date())

    def snooze(self, hours: int, now: datetime) -> datetime:
        if hours not in (1, 2, 5):
            raise StateError("告警静默时长只能是 1、2 或 5 小时")
        local = now
        self.snooze_until = local + timedelta(hours=hours)
        self._save(local.date())
        return self.snooze_until

    def alerts_snoozed(self, now: datetime) -> bool:
        return self.snooze_until is not None and now < self.snooze_until

    def _save(self, today: date) -> None:
        cutoff = (today - timedelta(days=7)).isoformat()
        self.completed = {
            item for item in self.completed
            if len(item) >= 10 and item[:10] >= cutoff
        }
        self.cancelled = {
            item for item in self.cancelled
            if len(item) >= 10 and item[:10] >= cutoff
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            temporary.write_text(
                json.dumps({
                    "initialized": self.initialized,
                    "completed": sorted(self.completed),
                    "cancelled": sorted(self.cancelled),
                    "alert-snooze-until": (
                        self.snooze_until.isoformat() if self.snooze_until else None
                    ),
                    "last-manual-power": (
                        {"action": self.manual_action, "at": self.manual_at.isoformat()}
                        if self.manual_action and self.manual_at else None
                    ),
                    "power-state": self.power_state,
                    "notified-power-state": self.notified_power_state,
                }, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self.path)
        except OSError as exc:
            raise StateError(f"调度状态无法写入：{exc}") from exc


LOGGER = logging.getLogger(__name__)
POLL_SECONDS = 30
Problem = tuple[str, str]


@dataclass(frozen=True)
class TickReport:
    status: InstanceStatus
    event: PowerEvent | None
    executed: bool = False
    transition: str | None = None


class MonitorService:
    def __init__(
        self, schedule: PowerSchedule, power, state: StateStore,
        *, heartbeat: Path | None = None,
        alert: Callable[[str, str], Awaitable[None]] | None = None,
        notify: Callable[[str, str], Awaitable[None]] | None = None,
        stop_packs: Callable[[], None] | None = None,
    ) -> None:
        self.schedule = schedule
        self.power = power
        self.state = state
        self.heartbeat = heartbeat
        self.alert = alert
        self.notify = notify
        self.stop_packs = stop_packs
        self.last_problem: Problem | None = None
        self.problem_alerted = False
        self.operation_lock = asyncio.Lock()

    async def run_once(self, now: datetime | None = None) -> TickReport:
        current_time = datetime.now(self.schedule.zone) if now is None else now.astimezone(self.schedule.zone)
        event = latest_event(current_time, self.schedule)
        async with self.operation_lock:
            status = self.power.status()
            transition = self.state.update_power_state(
                status.state, current_time.date(),
            )
            if not self.state.initialized:
                self.state.initialize(event, current_time.date())
                return TickReport(status, event, transition=transition)
            if event is None:
                return TickReport(status, event, transition=transition)
            if self.state.contains(event.key):
                # 取消的事件不再代表期望电源状态，不能产生 state-mismatch。
                return TickReport(
                    status, None if self.state.is_cancelled(event.key) else event,
                    transition=transition,
                )
            executed = await self._execute(event, status)
            if executed:
                self.state.mark(event.key, current_time.date())
            return TickReport(status, event, executed, transition)

    async def cancel_next_start(self, now: datetime | None = None) -> PowerEvent:
        current_time = datetime.now(self.schedule.zone) if now is None else now.astimezone(self.schedule.zone)
        event = next_start_event(current_time, self.schedule)
        if event is None:
            raise StateError("未启用自动开机预订")
        async with self.operation_lock:
            self.state.cancel(event.key, current_time.date())
        return event

    async def restore_next_start(self, now: datetime | None = None) -> PowerEvent:
        current_time = datetime.now(self.schedule.zone) if now is None else now.astimezone(self.schedule.zone)
        event = next_start_event(current_time, self.schedule)
        if event is None:
            raise StateError("未启用自动开机预订")
        async with self.operation_lock:
            self.state.restore(event.key, current_time.date())
        return event

    async def snooze_alerts(self, hours: int, now: datetime | None = None) -> datetime:
        current_time = datetime.now(self.schedule.zone) if now is None else now.astimezone(self.schedule.zone)
        async with self.operation_lock:
            return self.state.snooze(hours, current_time)

    async def record_manual_power(
        self, action: str, now: datetime | None = None,
    ) -> PowerEvent | None:
        """记录人工操作以解释后续告警，不改变任何定时事件。"""
        if action not in ("start", "stop"):
            raise StateError("人工电源操作只能是 start 或 stop")
        current_time = datetime.now(self.schedule.zone) if now is None else now.astimezone(self.schedule.zone)
        async with self.operation_lock:
            self.state.record_manual_power(action, current_time)
        LOGGER.info("已记录人工电源操作：%s", action)
        return latest_event(current_time, self.schedule)

    async def stop_all_packs(self) -> None:
        """关机前由 Manager 停止全部整合包；未配置时拒绝关机。"""
        if self.stop_packs is None:
            raise PowerError("未配置 Manager API，拒绝在未停止整合包的情况下关机")
        await asyncio.to_thread(self.stop_packs)

    async def _execute(self, event: PowerEvent, status: InstanceStatus) -> bool:
        if event.action == "start":
            if status.state in ("RUNNING", "STARTING"):
                return True
            if status.state != "STOPPED":
                raise PowerError(f"实例处于 {status.state}，暂不能执行定时开机")
            self.power.start()
            return True
        if status.state in ("STOPPED", "STOPPING"):
            return True
        if status.state != "RUNNING":
            raise PowerError(f"实例处于 {status.state}，暂不能执行定时关机")
        await self.stop_all_packs()
        self.power.stop(status)
        return True

    def _touch_heartbeat(self) -> None:
        if self.heartbeat is None:
            return
        self.heartbeat.parent.mkdir(parents=True, exist_ok=True)
        self.heartbeat.touch()

    def _problem(self, report: TickReport) -> Problem | None:
        if report.event is None or report.executed:
            return None
        expected = (
            ("RUNNING", "STARTING")
            if report.event.action == "start"
            else ("STOPPED", "STOPPING")
        )
        if report.status.state in expected:
            return None
        action = "开机" if report.event.action == "start" else "关机"
        manual = ""
        if (
            self.state.manual_action == "stop"
            and self.state.manual_at is not None
            and report.event.action == "start"
            and self.state.manual_at >= report.event.due_at
        ):
            manual = (
                f"你在 {self.state.manual_at:%H:%M} 人工提交了关机；"
                "当前仍处于自动开机时段。"
            )
        return (
            f"state-mismatch-{report.event.action}",
            f"{manual}计划期望实例已{action}，实际状态：{report.status.state}",
        )

    async def _set_problem(
        self, problem: Problem | None, now: datetime | None = None,
    ) -> None:
        current_time = datetime.now(self.schedule.zone) if now is None else now.astimezone(self.schedule.zone)
        if problem == self.last_problem and (
            problem is None or self.problem_alerted or self.state.alerts_snoozed(current_time)
        ):
            return
        previous = self.last_problem
        recovered = problem is None and previous is not None and self.problem_alerted
        self.last_problem = problem
        if problem is None:
            self.problem_alerted = False
        elif self.state.alerts_snoozed(current_time):
            self.problem_alerted = False
            LOGGER.info("告警已静默：%s", problem[0])
            return
        if recovered and self.state.alerts_snoozed(current_time):
            LOGGER.info("告警仍在静默期，不发送恢复通知")
            return
        if self.alert is not None and (problem is not None or recovered):
            try:
                if problem is None:
                    assert previous is not None
                    await self.alert("recovered", previous[1])
                else:
                    await self.alert(*problem)
                    self.problem_alerted = True
            except Exception:
                LOGGER.exception("提交主动告警失败")

    async def run(self, stopped: asyncio.Event, poll_seconds: int = POLL_SECONDS) -> None:
        while not stopped.is_set():
            try:
                report = await self.run_once()
                await self._set_problem(self._problem(report))
                if report.transition is not None and self.notify is not None:
                    try:
                        await self.notify(
                            report.transition.lower(), report.status.public_ip,
                        )
                        self.state.ack_power_notification(
                            report.transition, datetime.now(self.schedule.zone).date(),
                        )
                    except Exception:
                        LOGGER.exception("提交公开状态通知失败")
                if report.executed and report.event:
                    LOGGER.info("已提交定时%s；实例原状态=%s", report.event.action, report.status.state)
                else:
                    LOGGER.debug("实例状态=%s", report.status.state)
            except Exception as exc:
                LOGGER.exception("Monitor 检查失败，将在下一周期重试")
                await self._set_problem(("check-failed", f"Monitor 检查失败：{exc}"))
            finally:
                try:
                    self._touch_heartbeat()
                except OSError:
                    LOGGER.exception("无法更新健康状态")
            try:
                await asyncio.wait_for(stopped.wait(), timeout=poll_seconds)
            except asyncio.TimeoutError:
                pass
