from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

import yaml

from manager.lib.config import ManagerLimits
from manager.lib.modpacks import ModpackError, ModpackManager


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class FakeDocker:
    def __init__(self) -> None:
        self.active: set[str] = set()
        self.removed: list[str] = []

    def running(self, spec) -> bool:
        return spec.pack_id in self.active

    def start(self, spec, *, timeout: int) -> None:
        self.active.add(spec.pack_id)

    def stop(self, spec, *, timeout: int) -> None:
        self.active.discard(spec.pack_id)

    def remove_project(self, spec) -> None:
        self.removed.append(spec.pack_id)


class ManagerSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.packs = self.root / "modpacks"
        shutil.copytree(PROJECT_ROOT / "manager/modpacks/template", self.packs / "template")
        self.docker = FakeDocker()
        self.manager = ModpackManager(
            self.root, self.docker, ManagerLimits(1, "4g", 4 * 1024**3, 30, 30),
        )

    def make_pack(self, pack_id: str, port: int = 25565, memory: str = "2g") -> None:
        directory = self.manager.create(pack_id)
        config_file = directory / "config.yml"
        document = yaml.safe_load(config_file.read_text(encoding="utf-8"))
        document.update({"port": port, "mem-limit": memory})
        config_file.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    def test_running_limit_never_stops_another_pack_implicitly(self) -> None:
        self.make_pack("first")
        self.make_pack("second", port=25566)
        self.docker.active.add("first")
        with self.assertRaisesRegex(ModpackError, "最大运行数"):
            self.manager.start("second")
        self.assertEqual(self.docker.active, {"first"})

    def test_port_conflict_is_rejected(self) -> None:
        self.make_pack("first")
        self.make_pack("second")
        self.docker.active.add("first")
        with self.assertRaisesRegex(ModpackError, "端口 25565"):
            self.manager.start("second")

    def test_total_memory_limit_is_rejected(self) -> None:
        self.manager = ModpackManager(
            self.root, self.docker, ManagerLimits(2, "4g", 4 * 1024**3, 30, 30),
        )
        self.make_pack("first", memory="3g")
        self.make_pack("second", port=25566, memory="2g")
        self.docker.active.add("first")
        with self.assertRaisesRegex(ModpackError, "内存上限"):
            self.manager.start("second")

    def test_remove_requires_force_when_data_exists(self) -> None:
        self.make_pack("example")
        data = self.packs / "example/data"
        data.mkdir(exist_ok=True)
        (data / "world.dat").write_text("important", encoding="utf-8")
        with self.assertRaisesRegex(ModpackError, "--force"):
            self.manager.remove("example")
        self.assertTrue((self.packs / "example").exists())
        self.manager.remove("example", force=True)
        self.assertFalse((self.packs / "example").exists())


if __name__ == "__main__":
    unittest.main()
