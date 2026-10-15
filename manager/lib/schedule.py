"""User-cron integration and due automatic-backup execution."""

from __future__ import annotations

import fcntl
import shlex
import subprocess
import time
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .archive import list_backups
from .config import MANAGER_ROOT, PackSpec


CRON_MARKER = "# mc-serverpack-manager-schedule"


class ScheduleError(RuntimeError):
    pass


def backup_due(spec: PackSpec, now: float | None = None) -> bool:
    if not spec.backup_enabled:
        return False
    backups = list_backups(spec)
    if not backups:
        return True
    current = time.time() if now is None else now
    return current - backups[0].stat().st_mtime >= spec.backup_interval_seconds


@contextmanager
def schedule_lock(manager_root: Path = MANAGER_ROOT) -> Iterator[bool]:
    path = manager_root / ".schedule.lock"
    with path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def run_due(manager, now: float | None = None) -> tuple[list[Path], list[tuple[str, str]], bool]:
    """Run due backups; return completed paths, failures and whether the lock was acquired."""
    completed: list[Path] = []
    failed: list[tuple[str, str]] = []
    with schedule_lock(manager.manager_root) as acquired:
        if not acquired:
            return completed, failed, False
        for spec in manager.specs():
            if not backup_due(spec, now):
                continue
            try:
                completed.append(manager.backup(spec.pack_id))
            except Exception as exc:  # Independent packs must not prevent later backups.
                failed.append((spec.pack_id, str(exc)))
    return completed, failed, True


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
        result = runner(
            ["crontab", "-"], text=True, input=content, capture_output=True, check=False,
        )
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
