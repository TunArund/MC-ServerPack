from __future__ import annotations

import shutil
import tarfile
import tempfile
import unittest
from pathlib import Path

import yaml

from manager.lib.modpacks import ModpackError, ModpackManager
from manager.lib.transfer import export_pack, import_directory, import_pack


ROOT = Path(__file__).resolve().parents[1]


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
        archive = export_pack("source", self.packs)
        manager = ModpackManager(self.root)
        imported = import_pack(manager, "target", archive, force=False)
        (imported / "data/world.dat").write_text("keep", encoding="utf-8")
        (imported / "backups/old.tar.gz").write_text("keep", encoding="utf-8")
        (self.packs / "source/src/new.txt").write_text("new", encoding="utf-8")

        import_pack(manager, "target", export_pack("source", self.packs), force=True)

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
                export_pack("source", self.packs), force=True,
            )

    def test_directory_import_is_limited_to_managed_staging_and_preserves_data(self) -> None:
        manager = ModpackManager(self.root)
        staging = self.root / "tmp/transfers/target"
        shutil.copytree(self.packs / "source", staging)
        imported = import_directory(manager, "target", staging)
        (imported / "data/world.dat").write_text("keep", encoding="utf-8")
        (staging / "src/new.txt").write_text("new", encoding="utf-8")

        import_directory(manager, "target", staging)

        self.assertEqual((imported / "data/world.dat").read_text(), "keep")
        self.assertEqual((imported / "src/new.txt").read_text(), "new")
        with self.assertRaisesRegex(ModpackError, "受管范围"):
            import_directory(manager, "other", self.packs / "source")

    def test_directory_update_rolls_back_when_new_config_cannot_render(self) -> None:
        manager = ModpackManager(self.root)
        staging = self.root / "tmp/transfers/target"
        shutil.copytree(self.packs / "source", staging)
        imported = import_directory(manager, "target", staging)
        old_config = (imported / "config.yml").read_text(encoding="utf-8")
        (staging / "src/server.jar").write_bytes(b"new jar")
        document = yaml.safe_load((staging / "config.yml").read_text(encoding="utf-8"))
        document["read-only"] = {"enabled": True, "persistent": ["missing.txt"]}
        (staging / "config.yml").write_text(
            yaml.safe_dump(document, sort_keys=False), encoding="utf-8",
        )

        with self.assertRaisesRegex(Exception, "持久化文件不存在"):
            import_directory(manager, "target", staging)

        self.assertEqual((imported / "src/server.jar").read_bytes(), b"jar")
        self.assertEqual((imported / "config.yml").read_text(encoding="utf-8"), old_config)


if __name__ == "__main__":
    unittest.main()
