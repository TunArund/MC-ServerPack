from __future__ import annotations

import io
import tarfile
import tempfile
import unittest
import shutil
from datetime import datetime, timedelta
from pathlib import Path

import yaml

from manager.src.backup import (
    BackupError, backup_id, create_backup, resolve_backup, restore_backup,
)
from manager.src.config import load_pack, render_compose


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class BackupTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        shutil.copytree(PROJECT_ROOT / "manager/runtime", self.root / "runtime")
        self.packs = self.root / "modpacks"
        directory = self.packs / "example"
        (directory / "src/world").mkdir(parents=True)
        (directory / "src/world/level.dat").write_text("original", encoding="utf-8")
        (directory / "config.yml").write_text(yaml.safe_dump({
            "name": "Example", "version": "1", "java": 17, "mem-limit": "2g", "port": 25565,
            "read-only": {"enabled": True, "persistent": ["world/"]},
            "backup": {"enabled": False, "retain": 2, "quiesce-seconds": 0},
        }), encoding="utf-8")
        self.spec = load_pack("example", self.packs)
        render_compose(self.spec)

    def test_backup_restore_round_trip_and_retention(self) -> None:
        beginning = datetime(2026, 1, 1)
        first = create_backup(self.spec, beginning)
        world = self.spec.directory / "data/world/level.dat"
        world.write_text("changed", encoding="utf-8")
        create_backup(self.spec, beginning + timedelta(seconds=1))
        create_backup(self.spec, beginning + timedelta(seconds=2))

        self.assertFalse(first.exists())
        restore_backup(self.spec)
        self.assertEqual(world.read_text(encoding="utf-8"), "changed")
        self.assertEqual(len(list((self.spec.directory / "backups").glob("*.tar.gz"))), 2)

    def test_backup_can_be_resolved_by_stable_short_id(self) -> None:
        backup = create_backup(self.spec)

        self.assertEqual(resolve_backup(self.spec, backup_id(backup)), backup)
        self.assertEqual(resolve_backup(self.spec, backup_id(backup)[:4]), backup)
        self.assertEqual(len(backup_id(backup)), 12)

    def test_restore_rejects_path_traversal(self) -> None:
        backup = self.spec.directory / "backups/example-malicious.tar.gz"
        backup.parent.mkdir()
        with tarfile.open(backup, "w:gz") as archive:
            item = tarfile.TarInfo("data/../../escaped")
            payload = b"bad"
            item.size = len(payload)
            archive.addfile(item, io.BytesIO(payload))
        with self.assertRaisesRegex(BackupError, "不安全路径"):
            restore_backup(self.spec, backup.name)
        self.assertFalse((self.root / "escaped").exists())


if __name__ == "__main__":
    unittest.main()
