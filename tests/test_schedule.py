from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from manager.lib.schedule import CRON_MARKER, backup_due, install, remove, run_due, schedule_lock


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

    def specs(self):
        return self._specs

    def backup(self, pack_id: str) -> Path:
        self.backed_up.append(pack_id)
        return self.manager_root / f"{pack_id}.tar.gz"


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
