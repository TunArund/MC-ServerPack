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

from .power import InstanceStatus, PowerError

ZONE = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class PowerSchedule:
    auto_start: bool
    start_at: str | None
    auto_stop: bool
    stop_at: str | None


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
    local = now.astimezone(ZONE)
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


class StateError(RuntimeError):
    pass


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.completed = self._load()

    def _load(self) -> set[str]:
        try:
            content = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return set()
        except OSError as exc:
            raise StateError(f"调度状态无法读取：{exc}") from exc
        try:
            value = json.loads(content)
        except ValueError as exc:
            raise StateError(f"调度状态格式无效：{exc}") from exc
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise StateError("调度状态必须是字符串数组")
        return set(value)

    def contains(self, key: str) -> bool:
        return key in self.completed

    def mark(self, key: str, today: date) -> None:
        cutoff = (today - timedelta(days=7)).isoformat()
        self.completed = {
            item for item in self.completed
            if len(item) >= 10 and item[:10] >= cutoff
        }
        self.completed.add(key)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            temporary.write_text(
                json.dumps(sorted(self.completed), ensure_ascii=False) + "\n",
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


class MonitorService:
    def __init__(
        self, schedule: PowerSchedule, power, state: StateStore,
        *, heartbeat: Path | None = None,
        alert: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> None:
        self.schedule = schedule
        self.power = power
        self.state = state
        self.heartbeat = heartbeat
        self.alert = alert
        self.last_problem: Problem | None = None
        self.operation_lock = asyncio.Lock()

    async def run_once(self, now: datetime | None = None) -> TickReport:
        current_time = datetime.now(ZONE) if now is None else now.astimezone(ZONE)
        event = latest_event(current_time, self.schedule)
        async with self.operation_lock:
            status = self.power.status()
            if event is None or self.state.contains(event.key):
                return TickReport(status, event)
            executed = self._execute(event, status)
            if executed:
                self.state.mark(event.key, current_time.date())
            return TickReport(status, event, executed)

    def _execute(self, event: PowerEvent, status: InstanceStatus) -> bool:
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
        self.power.stop(status)
        return True

    def _touch_heartbeat(self) -> None:
        if self.heartbeat is None:
            return
        self.heartbeat.parent.mkdir(parents=True, exist_ok=True)
        self.heartbeat.touch()

    @staticmethod
    def _problem(report: TickReport) -> Problem | None:
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
        return (
            "state-mismatch",
            f"定时{action}后实例状态异常：{report.status.state}",
        )

    async def _set_problem(self, problem: Problem | None) -> None:
        if problem == self.last_problem:
            return
        recovered = problem is None and self.last_problem is not None
        self.last_problem = problem
        if self.alert is not None and (problem is not None or recovered):
            try:
                if problem is None:
                    await self.alert("recovered", "")
                else:
                    await self.alert(*problem)
            except Exception:
                LOGGER.exception("提交主动告警失败")

    async def run(self, stopped: asyncio.Event, poll_seconds: int = POLL_SECONDS) -> None:
        while not stopped.is_set():
            try:
                report = await self.run_once()
                await self._set_problem(self._problem(report))
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
