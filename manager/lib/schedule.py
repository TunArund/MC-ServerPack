"""One aggregated Manager schedule for shutdown actions and per-pack backups."""

from __future__ import annotations

import fcntl
import json
import os
import shlex
import subprocess
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import IO, Iterator
from zoneinfo import ZoneInfo

from .archive import list_backups
from .config import MANAGER_ROOT, ManagerLimits, PackSpec
from .power import create_power


ZONE = ZoneInfo("Asia/Shanghai")
CRON_MARKER = "# mc-serverpack-manager-schedule"
STATE_FILE = ".schedule-state.json"


class ScheduleError(RuntimeError):
    pass


@dataclass
class ScheduleReport:
    acquired: bool = True
    broadcasts: list[int] = field(default_factory=list)
    stopped: list[str] = field(default_factory=list)
    backups: list[Path] = field(default_factory=list)
    poweroff: bool = False
    failures: list[tuple[str, str]] = field(default_factory=list)


def reminders(first: int, step: int) -> tuple[int, ...]:
    result = []
    while first >= 1:
        result.append(first)
        first //= step
    return tuple(result)


def backup_due(spec: PackSpec, now: float | None = None) -> bool:
    if not spec.backup_enabled:
        return False
    backups = list_backups(spec)
    if not backups:
        return True
    current = time.time() if now is None else now
    return current - backups[0].stat().st_mtime >= spec.backup_interval_seconds


@contextmanager
def schedule_lock(manager_root: Path = MANAGER_ROOT) -> Iterator[IO[str] | None]:
    path = manager_root / STATE_FILE
    with path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield None
            return
        try:
            handle.seek(0)
            if not handle.read().strip():
                handle.seek(0)
                handle.write("[]\n")
                handle.flush()
                os.fsync(handle.fileno())
            yield handle
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _load_state(handle: IO[str], date: str) -> set[str]:
    try:
        handle.seek(0)
        content = handle.read()
        value = json.loads(content) if content.strip() else []
    except (OSError, ValueError) as exc:
        raise ScheduleError(f"调度状态无法读取：{exc}") from exc
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ScheduleError("调度状态格式无效")
    return {item for item in value if item.startswith(date + ":")}


def _save_state(handle: IO[str], state: set[str]) -> None:
    try:
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps(sorted(state), ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    except OSError as exc:
        raise ScheduleError(f"调度状态无法写入：{exc}") from exc


def _stop_time(now: datetime, text: str) -> datetime:
    hour, minute = (int(part) for part in text.split(":"))
    return now.astimezone(ZONE).replace(hour=hour, minute=minute, second=0, microsecond=0)


def _power_time(stop_at: datetime, settings: ManagerLimits) -> datetime:
    result = _stop_time(stop_at, settings.poweroff_at)
    if result < stop_at:
        result += timedelta(days=1)
    return result


def _event_end(stop_at: datetime, settings: ManagerLimits) -> datetime:
    if settings.poweroff_enabled:
        return _power_time(stop_at, settings) + timedelta(minutes=settings.poweroff_deadline)
    return stop_at + timedelta(minutes=settings.poweroff_deadline)


def _scheduled_stop(now: datetime, settings: ManagerLimits) -> datetime:
    local = now.astimezone(ZONE)
    today = _stop_time(local, settings.stop_at)
    previous = today - timedelta(days=1)
    if local < today and local <= _event_end(previous, settings):
        return previous
    return today


def _run_shutdown(
    manager, now: datetime, state: set[str], report: ScheduleReport,
    power_factory, state_handle: IO[str],
) -> None:
    settings: ManagerLimits = manager.limits
    if not settings.schedule_enabled:
        return
    local = now.astimezone(ZONE).replace(second=0, microsecond=0)
    stop_at = _scheduled_stop(local, settings)
    date = stop_at.date().isoformat()
    remaining = int((stop_at - local).total_seconds() // 60)

    if remaining in reminders(settings.notify_elast, settings.notify_step):
        key = f"{date}:broadcast:{remaining}"
        if key not in state:
            sent, failed = manager.broadcast_all(remaining)
            report.broadcasts.append(remaining)
            report.failures.extend((f"broadcast:{pack_id}", reason) for pack_id, reason in failed)
            if not failed:
                state.add(key)
                _save_state(state_handle, state)

    if not (stop_at <= local <= _event_end(stop_at, settings)):
        return
    stop_key = f"{date}:stop-all"
    if stop_key not in state:
        stopped, failed = manager.stop_all()
        report.stopped.extend(stopped)
        report.failures.extend((f"stop:{pack_id}", reason) for pack_id, reason in failed)
        if failed:
            return
        state.add(stop_key)
        _save_state(state_handle, state)

    if not settings.poweroff_enabled:
        return
    power_at = _power_time(stop_at, settings)
    if not (power_at <= local <= power_at + timedelta(minutes=settings.poweroff_deadline)):
        return
    power_key = f"{date}:poweroff"
    if power_key in state:
        return
    try:
        power_factory(settings, manager.manager_root).stop()
    except Exception as exc:
        report.failures.append(("poweroff", str(exc)))
        return
    report.poweroff = True
    state.add(power_key)
    _save_state(state_handle, state)


def _run_backups(manager, now_timestamp: float, report: ScheduleReport) -> None:
    for spec in manager.specs():
        if not backup_due(spec, now_timestamp):
            continue
        try:
            report.backups.append(manager.backup(spec.pack_id))
        except Exception as exc:  # Independent packs must not prevent later backups.
            report.failures.append((f"backup:{spec.pack_id}", str(exc)))


def run_all(
    manager, now: datetime | None = None,
    power_factory: Callable[..., object] = create_power,
) -> ScheduleReport:
    current = datetime.now(ZONE) if now is None else now.astimezone(ZONE)
    report = ScheduleReport()
    with schedule_lock(manager.manager_root) as state_handle:
        report.acquired = state_handle is not None
        if state_handle is None:
            return report
        event = _scheduled_stop(current, manager.limits) if manager.limits.schedule_enabled else current
        date = event.date().isoformat()
        state = _load_state(state_handle, date)
        _run_shutdown(manager, current, state, report, power_factory, state_handle)
        # Never begin a potentially long backup once the shutdown window starts.
        if not manager.limits.schedule_enabled or current < _scheduled_stop(current, manager.limits):
            _run_backups(manager, current.timestamp(), report)
    return report


def run_due(manager, now: float | None = None) -> tuple[list[Path], list[tuple[str, str]], bool]:
    """Compatibility helper for tests and backup-only callers."""
    report = ScheduleReport()
    with schedule_lock(manager.manager_root) as state_handle:
        report.acquired = state_handle is not None
        if state_handle is not None:
            _run_backups(manager, time.time() if now is None else now, report)
    failures = [(name.removeprefix("backup:"), reason) for name, reason in report.failures]
    return report.backups, failures, report.acquired


Runner = Callable[..., subprocess.CompletedProcess[str]]


def _read_crontab(runner: Runner) -> list[str]:
    try:
        result = runner(["crontab", "-l"], text=True, capture_output=True, check=False)
    except OSError as exc:
        raise ScheduleError(f"无法执行 crontab：{exc}") from exc
    if result.returncode == 0:
        return result.stdout.splitlines()
    if result.returncode == 1:
        return []
    raise ScheduleError(f"读取 crontab 失败：{result.stderr.strip()}")


def _write_crontab(lines: list[str], runner: Runner) -> None:
    content = "\n".join(lines)
    if content:
        content += "\n"
    try:
        result = runner(["crontab", "-"], text=True, input=content, capture_output=True, check=False)
    except OSError as exc:
        raise ScheduleError(f"无法执行 crontab：{exc}") from exc
    if result.returncode:
        raise ScheduleError(f"写入 crontab 失败：{result.stderr.strip()}")


def install(manager_root: Path = MANAGER_ROOT, runner: Runner = subprocess.run) -> None:
    lines = [line for line in _read_crontab(runner) if CRON_MARKER not in line]
    root = shlex.quote(str(manager_root))
    log = shlex.quote(str(manager_root / "schedule.log"))
    lines.append(f"* * * * * cd {root} && ./manager.sh schedule run >> {log} 2>&1 {CRON_MARKER}")
    _write_crontab(lines, runner)


def remove(runner: Runner = subprocess.run) -> None:
    lines = [line for line in _read_crontab(runner) if CRON_MARKER not in line]
    _write_crontab(lines, runner)
