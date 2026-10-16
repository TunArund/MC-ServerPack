"""Daily power-event calculations and atomic completion state."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

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
