from __future__ import annotations

import shutil
import tarfile
import tempfile
import unittest
from pathlib import Path

from lib.modpack_sdk import ModpackError, ModpackWorkspace


ROOT = Path(__file__).resolve().parents[2]


class ModpackWorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.packs = Path(temporary.name) / "modpacks"
        shutil.copytree(ROOT / "terminal/modpacks/template", self.packs / "template")
        self.workspace = ModpackWorkspace(self.packs)

    def test_create_check_export_remove_import(self) -> None:
        created = self.workspace.create("Example-01")
        (created / "src/server.jar").write_bytes(b"jar")
        details = self.workspace.check("Example-01")
        self.assertEqual(details["id"], "Example-01")
        self.assertEqual(details["name"], "Example-01")

        archive = self.workspace.export("Example-01")
        self.assertEqual(archive, self.packs / "Example-01.tgz")
        self.workspace.remove("Example-01")
        imported = self.workspace.import_archive("Example-01")

        self.assertEqual((imported / "src/server.jar").read_bytes(), b"jar")
        self.assertFalse((imported / "manifest.json").exists())

    def test_list_ignores_template_and_archives(self) -> None:
        self.workspace.create("One")
        self.assertEqual([item["id"] for item in self.workspace.list()], ["One"])

    def test_import_rejects_traversal(self) -> None:
        archive = self.packs / "Unsafe.tgz"
        payload = self.packs / "payload"
        payload.write_text("bad", encoding="utf-8")
        with tarfile.open(archive, "w:gz") as output:
            output.add(payload, arcname="../escape")
        with self.assertRaisesRegex(ModpackError, "不安全路径"):
            self.workspace.import_archive("Unsafe")


if __name__ == "__main__":
    unittest.main()
