"""Pure calculations for daily Monitor power events."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .config import ScheduleConfig


ZONE = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class PowerEvent:
    key: str
    action: str
    due_at: datetime


def _at(local: datetime, clock: str) -> datetime:
    hour, minute = (int(part) for part in clock.split(":"))
    return local.replace(hour=hour, minute=minute, second=0, microsecond=0)


def latest_event(now: datetime, schedule: ScheduleConfig) -> PowerEvent | None:
    """Return the most recent enabled event, including yesterday for catch-up."""
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
