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
        self.commands: list[tuple[str, str]] = []

    def running(self, spec) -> bool:
        return spec.pack_id in self.active

    def start(self, spec, *, timeout: int) -> None:
        self.active.add(spec.pack_id)

    def stop(self, spec, *, timeout: int) -> None:
        self.active.discard(spec.pack_id)

    def remove_project(self, spec) -> None:
        self.removed.append(spec.pack_id)

    def console(self, spec, command: str) -> None:
        self.commands.append((spec.pack_id, command))


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
        document.update({"port": port, "mem-limit": memory, "eula": True})
        config_file.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
        (directory / "src/server.jar").write_bytes(b"test server")

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

    def test_broadcast_only_targets_running_packs(self) -> None:
        self.make_pack("running")
        self.make_pack("stopped", port=25566)
        self.docker.active.add("running")
        sent, failed = self.manager.broadcast_all(15)
        self.assertEqual(sent, ["running"])
        self.assertFalse(failed)
        self.assertEqual(self.docker.commands, [("running", "say 服务器将在 15 分钟后关闭")])

    def test_list_packs_excludes_template_and_reports_running_state(self) -> None:
        self.make_pack("alpha")
        self.make_pack("beta", port=25566)
        self.docker.active.add("beta")

        listed = self.manager.list_packs()

        self.assertEqual(
            [(spec.pack_id, running) for spec, running in listed],
            [("alpha", False), ("beta", True)],
        )

    def test_start_writes_eula_after_source_was_replaced(self) -> None:
        self.make_pack("server")
        source_eula = self.packs / "server/src/eula.txt"
        source_eula.write_text("eula=false\n", encoding="utf-8")

        self.manager.start("server")

        self.assertEqual(source_eula.read_text(encoding="utf-8"), "eula=true\n")
        self.assertEqual(self.docker.active, {"server"})

    def test_start_updates_persisted_eula_when_present(self) -> None:
        self.make_pack("server")
        config_file = self.packs / "server/config.yml"
        document = yaml.safe_load(config_file.read_text(encoding="utf-8"))
        document["read-only"]["persistent"].append("eula.txt")
        config_file.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
        persisted = self.packs / "server/data/eula.txt"
        persisted.parent.mkdir(exist_ok=True)
        persisted.write_text("eula=false\n", encoding="utf-8")

        self.manager.start("server")

        self.assertEqual(persisted.read_text(encoding="utf-8"), "eula=true\n")

    def test_start_sets_configured_server_port_without_template_properties(self) -> None:
        self.make_pack("server", port=25566)

        self.manager.start("server")

        properties = self.packs / "server/src/server.properties"
        self.assertEqual(properties.read_text(encoding="utf-8"), "server-port=25566\n")

    def test_start_updates_only_server_port_in_existing_properties(self) -> None:
        self.make_pack("server", port=25566)
        properties = self.packs / "server/src/server.properties"
        properties.write_text(
            "motd=Hello\nserver-port=25565\nonline-mode=true\n",
            encoding="utf-8",
        )

        self.manager.start("server")

        self.assertEqual(
            properties.read_text(encoding="utf-8"),
            "motd=Hello\nserver-port=25566\nonline-mode=true\n",
        )

    def test_property_uses_persistent_server_properties_when_configured(self) -> None:
        self.make_pack("server")
        config_file = self.packs / "server/config.yml"
        document = yaml.safe_load(config_file.read_text(encoding="utf-8"))
        document["read-only"]["persistent"].append("server.properties")
        config_file.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
        persisted = self.packs / "server/data/server.properties"
        persisted.parent.mkdir(exist_ok=True)
        persisted.write_text("online-mode=true\nmotd=Hello\n", encoding="utf-8")

        target, running = self.manager.set_property(
            "server", "online-mode", "false",
        )
        value, read_target = self.manager.get_property("server", "online-mode")

        self.assertEqual(target, persisted)
        self.assertEqual(read_target, persisted)
        self.assertEqual(value, "false")
        self.assertFalse(running)
        self.assertEqual(
            persisted.read_text(encoding="utf-8"),
            "online-mode=false\nmotd=Hello\n",
        )

    def test_property_rejects_newline_and_unsafe_key(self) -> None:
        self.make_pack("server")
        with self.assertRaisesRegex(ModpackError, "属性名"):
            self.manager.set_property("server", "../online-mode", "false")
        with self.assertRaisesRegex(ModpackError, "换行"):
            self.manager.set_property("server", "motd", "hello\noperator=true")

    def test_start_requires_eula_in_pack_config(self) -> None:
        self.make_pack("server")
        config_file = self.packs / "server/config.yml"
        document = yaml.safe_load(config_file.read_text(encoding="utf-8"))
        document["eula"] = False
        config_file.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

        with self.assertRaisesRegex(ModpackError, "eula: true"):
            self.manager.start("server")

    def test_start_reports_missing_server_jar_before_docker(self) -> None:
        self.make_pack("server")
        (self.packs / "server/src/server.jar").unlink()

        with self.assertRaisesRegex(ModpackError, "当前找到 0 个"):
            self.manager.start("server")


if __name__ == "__main__":
    unittest.main()
