from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from manager.lib.config import ManagerLimits
from manager.lib.schedule import CRON_MARKER, backup_due, install, reminders, remove, run_all, run_due, schedule_lock


class FakeCrontab:
    def __init__(self, content: str = "") -> None:
        self.content = content
        self.writes = 0

    def __call__(self, arguments: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        if arguments == ["crontab", "-l"]:
            if not self.content:
                return subprocess.CompletedProcess(arguments, 1, "", "no crontab")
            return subprocess.CompletedProcess(arguments, 0, self.content, "")
        if arguments == ["crontab", "-"]:
            self.content = kwargs["input"]
            self.writes += 1
            return subprocess.CompletedProcess(arguments, 0, "", "")
        raise AssertionError(arguments)


class FakeManager:
    def __init__(self, root: Path, specs: list[SimpleNamespace]) -> None:
        self.manager_root = root
        self._specs = specs
        self.backed_up: list[str] = []
        self.limits = ManagerLimits(1, "4g", 4 * 1024**3, 30, 30)
        self.broadcasts: list[int] = []
        self.stop_calls = 0

    def specs(self):
        return self._specs

    def backup(self, pack_id: str) -> Path:
        self.backed_up.append(pack_id)
        return self.manager_root / f"{pack_id}.tar.gz"

    def broadcast_all(self, minutes: int):
        self.broadcasts.append(minutes)
        return ["example"], []

    def stop_all(self):
        self.stop_calls += 1
        return ["example"], []


class PartlyFailingManager(FakeManager):
    def backup(self, pack_id: str) -> Path:
        if pack_id == "broken":
            raise RuntimeError("disk full")
        return super().backup(pack_id)


class ScheduleTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def spec(self, pack_id: str = "example", *, enabled: bool = True, interval: int = 3600):
        directory = self.root / "modpacks" / pack_id
        (directory / "backups").mkdir(parents=True)
        return SimpleNamespace(
            pack_id=pack_id, directory=directory, backup_enabled=enabled,
            backup_interval_seconds=interval,
        )

    def test_due_uses_latest_backup_time(self) -> None:
        spec = self.spec()
        self.assertTrue(backup_due(spec, now=10_000))
        backup = spec.directory / "backups/example-1.tar.gz"
        backup.touch()
        os.utime(backup, (9_500, 9_500))
        self.assertFalse(backup_due(spec, now=10_000))
        self.assertTrue(backup_due(spec, now=13_100))
        spec.backup_enabled = False
        self.assertFalse(backup_due(spec, now=13_100))

    def test_reminder_sequence(self) -> None:
        self.assertEqual(reminders(60, 2), (60, 30, 15, 7, 3, 1))

    def test_run_due_only_backs_up_due_packs(self) -> None:
        due = self.spec("due")
        disabled = self.spec("disabled", enabled=False)
        manager = FakeManager(self.root, [due, disabled])
        completed, failed, acquired = run_due(manager, now=10_000)
        self.assertTrue(acquired)
        self.assertFalse(failed)
        self.assertEqual(manager.backed_up, ["due"])
        self.assertEqual(completed, [self.root / "due.tar.gz"])

    def test_failure_does_not_block_other_packs(self) -> None:
        manager = PartlyFailingManager(self.root, [self.spec("broken"), self.spec("healthy")])
        completed, failed, acquired = run_due(manager, now=10_000)
        self.assertTrue(acquired)
        self.assertEqual(completed, [self.root / "healthy.tar.gz"])
        self.assertEqual(failed, [("broken", "disk full")])

    def test_overlapping_run_is_skipped(self) -> None:
        manager = FakeManager(self.root, [self.spec()])
        with schedule_lock(self.root) as acquired:
            self.assertTrue(acquired)
            completed, failed, second_acquired = run_due(manager, now=10_000)
        self.assertFalse(second_acquired)
        self.assertFalse(completed)
        self.assertFalse(failed)

    def test_shutdown_timeline_is_deduplicated(self) -> None:
        manager = FakeManager(self.root, [])
        manager.limits = ManagerLimits(
            1, "4g", 4 * 1024**3, 30, 30, True, "23:00", 60, 2,
        )
        zone = ZoneInfo("Asia/Shanghai")
        first = run_all(manager, datetime(2026, 10, 16, 22, 0, tzinfo=zone))
        duplicate = run_all(manager, datetime(2026, 10, 16, 22, 0, 30, tzinfo=zone))
        stopped = run_all(manager, datetime(2026, 10, 16, 23, 0, tzinfo=zone))
        self.assertEqual(first.broadcasts, [60])
        self.assertFalse(duplicate.broadcasts)
        self.assertEqual(manager.broadcasts, [60])
        self.assertEqual(stopped.stopped, ["example"])
        self.assertEqual(manager.stop_calls, 1)

    def test_shutdown_window_crosses_midnight(self) -> None:
        manager = FakeManager(self.root, [])
        manager.limits = ManagerLimits(
            1, "4g", 4 * 1024**3, 30, 30, True, "23:59", 60, 2,
            False, 5,
        )
        report = run_all(
            manager, datetime(2026, 10, 17, 0, 2, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
        duplicate = run_all(
            manager, datetime(2026, 10, 17, 0, 3, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
        self.assertEqual(report.stopped, ["example"])
        self.assertFalse(duplicate.stopped)
        self.assertEqual(manager.stop_calls, 1)

    def test_poweroff_is_retried_and_then_deduplicated(self) -> None:
        manager = FakeManager(self.root, [])
        manager.limits = ManagerLimits(
            1, "4g", 4 * 1024**3, 30, 30, True, "23:00", 60, 2,
            True, 5, "ap-test", "ins-test",
        )
        calls: list[str] = []

        class Power:
            def stop(self):
                calls.append("stop")

        factory = lambda settings, root: Power()
        now = datetime(2026, 10, 16, 23, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        first = run_all(manager, now, factory)
        second = run_all(manager, now, factory)
        self.assertTrue(first.poweroff)
        self.assertFalse(second.poweroff)
        self.assertEqual(calls, ["stop"])

    def test_cron_install_is_idempotent_and_preserves_other_entries(self) -> None:
        runner = FakeCrontab("0 1 * * * other-job\n")
        install(self.root, runner)
        install(self.root, runner)
        self.assertIn("other-job", runner.content)
        self.assertEqual(runner.content.count(CRON_MARKER), 1)
        self.assertIn("./manager.sh schedule run", runner.content)
        remove(runner)
        self.assertIn("other-job", runner.content)
        self.assertNotIn(CRON_MARKER, runner.content)


if __name__ == "__main__":
    unittest.main()
