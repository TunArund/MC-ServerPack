from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from manager.lib.config import ConfigError, load_pack, normalize_persistent, render_compose
from manager.lib.docker import DockerClient
from manager.lib.modpacks import ModpackManager


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ManagerFixture(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.packs = self.root / "modpacks"
        self.packs.mkdir()
        shutil.copytree(PROJECT_ROOT / "manager/runtime", self.root / "runtime")

    def make_pack(self, pack_id: str = "example") -> Path:
        directory = self.packs / pack_id
        source = directory / "src"
        (source / "world").mkdir(parents=True)
        (source / "world" / "level.dat").write_text("world", encoding="utf-8")
        (source / "eula.txt").write_text("eula=true\n", encoding="utf-8")
        document = {
            "name": "Example", "version": "1.0", "java": 17,
            "mem-limit": "4g", "port": 25565,
            "command": ["bash", "./run.sh", "nogui"],
            "read-only": {"enabled": True, "persistent": ["world/", "eula.txt"]},
        }
        (directory / "config.yml").write_text(
            yaml.safe_dump(document, sort_keys=False), encoding="utf-8",
        )
        return directory


class PackConfigTests(ManagerFixture):
    def test_render_prepares_persistent_data_and_is_idempotent(self) -> None:
        directory = self.make_pack()
        spec = load_pack("example", self.packs)
        target = render_compose(spec)
        first = target.read_text(encoding="utf-8")
        render_compose(spec)

        self.assertEqual((directory / "data/world/level.dat").read_text(), "world")
        self.assertEqual((directory / "data/eula.txt").read_text(), "eula=true\n")
        self.assertEqual(target.read_text(encoding="utf-8"), first)
        compose = yaml.safe_load(first)
        service = compose["services"]["minecraft"]
        self.assertRegex(service["image"], r"^mc-java:17-[0-9a-f]{12}$")
        self.assertEqual(service["mem_limit"], "4g")
        self.assertTrue(service["volumes"][0]["read_only"])

    def test_rejects_path_traversal(self) -> None:
        for value in ("../world", "/etc/passwd", "world/../ops.json"):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                normalize_persistent(value)

    def test_create_uses_template_without_copying_generated_compose(self) -> None:
        shutil.copytree(PROJECT_ROOT / "manager/modpacks/template", self.packs / "template")
        (self.packs / "template/compose.yml").write_text("stale", encoding="utf-8")
        manager = ModpackManager(self.root)

        target = manager.create("new-pack")

        self.assertFalse((target / "compose.yml").exists())
        self.assertIn("name: new-pack", (target / "config.yml").read_text(encoding="utf-8"))
        self.assertTrue((target / "backups").is_dir())


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict]] = []
        self.running = False

    def __call__(self, arguments: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        self.calls.append((arguments, kwargs))
        output, error, code = "", "", 0
        if arguments[:3] == ["docker", "image", "inspect"]:
            code = 1
        elif "--status" in arguments and "running" in arguments:
            output = "container-id\n" if self.running else ""
        elif arguments[:2] == ["docker", "inspect"]:
            output = "0\n"
        elif "up" in arguments:
            self.running = True
        elif "stop" in arguments:
            self.running = False
        elif "ps" in arguments and "-a" in arguments:
            output = "container-id\n"
        return subprocess.CompletedProcess(arguments, code, output, error)


class DockerClientTests(ManagerFixture):
    def setUp(self) -> None:
        super().setUp()
        self.make_pack()
        self.spec = load_pack("example", self.packs)
        self.runner = FakeRunner()
        self.client = DockerClient(self.root, self.runner)
        self.environment = patch.dict(os.environ, {"MC_CONTAINER_UID": "1000", "MC_CONTAINER_GID": "1000"})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_start_builds_missing_image_then_waits_for_health(self) -> None:
        self.client.start(self.spec)
        commands = [call[0] for call in self.runner.calls]
        self.assertTrue(any(command[:2] == ["docker", "build"] for command in commands))
        up = next(command for command in commands if "up" in command)
        self.assertEqual(up[-4:], ["-d", "--wait", "--wait-timeout", "300"])

    def test_start_is_idempotent_when_already_running(self) -> None:
        self.runner.running = True
        self.client.start(self.spec)
        self.assertFalse(any("build" in command or "up" in command for command, _ in self.runner.calls))

    def test_console_passes_user_text_only_through_stdin(self) -> None:
        self.runner.running = True
        text = "say hello; stop"
        self.client.console(self.spec, text)
        command, options = self.runner.calls[-1]
        self.assertNotIn(text, command)
        self.assertEqual(options["input"], text + "\n")

    def test_stop_checks_for_clean_container_exit(self) -> None:
        self.runner.running = True
        self.client.stop(self.spec)
        commands = [call[0] for call in self.runner.calls]
        self.assertTrue(any("stop" in command for command in commands))
        self.assertTrue(any(command[:2] == ["docker", "inspect"] for command in commands))


if __name__ == "__main__":
    unittest.main()
