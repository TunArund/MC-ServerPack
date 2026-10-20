from __future__ import annotations

import shutil
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import yaml

from manager.src.modpacks import ModpackError, ModpackManager
from manager.src.transfer import TransferStore, export_pack, import_pack


ROOT = Path(__file__).resolve().parents[2]


class ManagerTransferTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.packs = self.root / "modpacks"
        source = self.packs / "source"
        (source / "src").mkdir(parents=True)
        (source / "src/server.jar").write_bytes(b"jar")
        (source / "config.yml").write_text(yaml.safe_dump({
            "name": "Source", "version": "1", "java": 17,
            "mem-limit": "4g", "port": 25565,
            "command": ["java", "-jar", "server.jar", "nogui"],
            "read-only": {"enabled": False, "persistent": []},
        }), encoding="utf-8")
        shutil.copytree(ROOT / "manager/runtime", self.root / "runtime")

    def test_export_import_preserves_existing_data_and_backups(self) -> None:
        archive = export_pack(
            "source", self.packs, mode="source-only",
            destination=self.root / "source.tgz",
        )
        destination = self.root / "destination"
        shutil.copytree(ROOT / "manager/runtime", destination / "runtime")
        manager = ModpackManager(destination)
        manager.docker.running = Mock(return_value=False)
        imported = import_pack(manager, "source", archive, force=False)
        (imported / "data/world.dat").write_text("keep", encoding="utf-8")
        (imported / "backups/old.tar.gz").write_text("keep", encoding="utf-8")
        (self.packs / "source/src/new.txt").write_text("new", encoding="utf-8")

        import_pack(manager, "source", export_pack(
            "source", self.packs, mode="source-only",
            destination=self.root / "replacement.tgz",
        ), force=True)

        self.assertEqual((imported / "data/world.dat").read_text(), "keep")
        self.assertEqual((imported / "backups/old.tar.gz").read_text(), "keep")
        self.assertEqual((imported / "src/new.txt").read_text(), "new")

    def test_import_rejects_links_and_path_traversal(self) -> None:
        archive = self.root / "unsafe.tar.gz"
        payload = self.root / "payload"
        payload.write_text("bad", encoding="utf-8")
        with tarfile.open(archive, "w:gz") as handle:
            handle.add(payload, arcname="../escape")
        with self.assertRaisesRegex(ModpackError, "不安全路径"):
            import_pack(ModpackManager(self.root), "target", archive, force=False)

    def test_force_import_rejects_symlink_target(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        (self.packs / "target").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ModpackError, "安全目录"):
            import_pack(
                ModpackManager(self.root), "target",
                export_pack(
                    "source", self.packs,
                    destination=self.root / "source.tgz",
                ), force=True,
            )

    def test_download_store_moves_archive_to_managed_storage(self) -> None:
        temporary_export = self.root / "mcsp-pack-test"
        temporary_export.mkdir()
        archive = temporary_export / "source.tgz"
        archive.write_bytes(b"archive")

        result = TransferStore(self.root / "manager").create_download(
            "source", archive,
        )

        self.assertTrue(temporary_export.exists())
        self.assertFalse(archive.exists())
        stored = self.root / "manager/data/transfers" / result["transfer_id"] / "archive.tgz"
        self.assertEqual(stored.read_bytes(), b"archive")


if __name__ == "__main__":
    unittest.main()
