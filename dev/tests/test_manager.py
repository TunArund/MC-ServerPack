from __future__ import annotations

import io
import os
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

import yaml

from manager.src.config import (
    ConfigError, load_pack, normalize_persistent, parse_interval, render_compose,
    validate_pack_id,
)
from manager.src.docker import DockerClient, DockerError
from manager.src.modpacks import ModpackError, ModpackManager
from manager.src.transfer import export_pack, import_pack


PROJECT_ROOT = Path(__file__).resolve().parents[2]


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
        (source / "server.jar").write_bytes(b"test server")
        document = {
            "name": "Example", "version": "1.0", "java": 17,
            "mem-limit": "4g", "port": 25565,
            "eula": True,
            "jvm-args": ["-Xms1G", "-Xmx4G", "-XX:+UseG1GC"],
            "command": ["java", "-jar", "server.jar", "nogui"],
            "read-only": {"enabled": True, "persistent": ["world/", "eula.txt"]},
        }
        (directory / "config.yml").write_text(
            yaml.safe_dump(document, sort_keys=False), encoding="utf-8",
        )
        return directory


class PackConfigTests(ManagerFixture):
    def test_pack_id_allows_only_ascii_letters_digits_and_hyphen(self) -> None:
        self.assertEqual(validate_pack_id("Pack-01"), "Pack-01")
        for invalid in ("pack_name", "pack.name", "../pack", "中文"):
            with self.subTest(invalid=invalid), self.assertRaises(ConfigError):
                validate_pack_id(invalid)

    def test_backup_interval_units(self) -> None:
        self.assertEqual(parse_interval("30m"), ("30m", 1800))
        self.assertEqual(parse_interval("6H"), ("6h", 21600))
        self.assertEqual(parse_interval("1d"), ("1d", 86400))
        for value in ("0m", "30", "1w", 60):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                parse_interval(value)

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
        self.assertEqual(service["restart"], "no")
        self.assertEqual(
            service["user"],
            "${MC_CONTAINER_UID:-1000}:${MC_CONTAINER_GID:-1000}",
        )
        self.assertTrue(service["volumes"][0]["read_only"])
        self.assertEqual(
            service["command"],
            ["java", "-Xms1G", "-Xmx4G", "-XX:+UseG1GC", "-jar", "server.jar", "nogui"],
        )

    def test_render_repairs_empty_minecraft_json_files(self) -> None:
        directory = self.make_pack()
        config_file = directory / "config.yml"
        document = yaml.safe_load(config_file.read_text(encoding="utf-8"))
        document["read-only"]["persistent"].extend([
            "banned-players.json", "usernamecache.json",
        ])
        config_file.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
        (directory / "src/banned-players.json").write_text("", encoding="utf-8")
        (directory / "src/usernamecache.json").write_text("", encoding="utf-8")

        render_compose(load_pack("example", self.packs))

        self.assertEqual((directory / "src/banned-players.json").read_text(), "[]\n")
        self.assertEqual((directory / "data/banned-players.json").read_text(), "[]\n")
        self.assertEqual((directory / "src/usernamecache.json").read_text(), "{}\n")
        self.assertEqual((directory / "data/usernamecache.json").read_text(), "{}\n")

    def test_render_creates_user_owned_targets_for_persistent_files(self) -> None:
        directory = self.make_pack()
        config_file = directory / "config.yml"
        document = yaml.safe_load(config_file.read_text(encoding="utf-8"))
        document["read-only"]["persistent"].append("server.properties")
        config_file.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
        (directory / "data").mkdir()
        (directory / "data/server.properties").write_text(
            "server-port=25565\n", encoding="utf-8",
        )

        render_compose(load_pack("example", self.packs))

        self.assertTrue((directory / "src/server.properties").is_file())
        self.assertEqual((directory / "src/server.properties").read_text(), "")

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

    def test_template_uses_automatic_launch_without_editable_command(self) -> None:
        document = yaml.safe_load(
            (PROJECT_ROOT / "manager/modpacks/template/config.yml").read_text(encoding="utf-8")
        )
        self.assertNotIn("command", document)
        self.assertNotIn("jvm-args", document)
        self.assertIs(document["eula"], False)
        template_source = PROJECT_ROOT / "manager/modpacks/template/src"
        for legacy in ("run.sh", "run.bat", "user_jvm_args.txt"):
            self.assertFalse((template_source / legacy).exists())

    def test_runtime_auto_launches_the_only_server_jar(self) -> None:
        server = self.root / "server"
        binaries = self.root / "bin"
        server.mkdir()
        binaries.mkdir()
        with zipfile.ZipFile(server / "minecraft-1.20.1.jar", "w") as archive:
            archive.writestr("META-INF/MANIFEST.MF", "Main-Class: net.minecraft.Main\n")
        java = binaries / "java"
        java.write_text(
            "#!/usr/bin/env bash\nprintf '%s\\n' \"$@\" > \"$MC_SERVER_DIR/java-args.txt\"\n",
            encoding="utf-8",
        )
        java.chmod(0o755)
        environment = os.environ.copy()
        environment.update({
            "PATH": f"{binaries}:{environment['PATH']}",
            "MC_SERVER_DIR": str(server),
            "MC_CONSOLE_PIPE": str(self.root / "console.pipe"),
        })

        subprocess.run(
            ["bash", str(PROJECT_ROOT / "manager/runtime/entrypoint.sh"), "mc-auto"],
            env=environment, check=True, capture_output=True, text=True,
        )

        self.assertEqual(
            (server / "java-args.txt").read_text(encoding="utf-8").splitlines(),
            [
                "-XX:InitialRAMPercentage=25.0", "-XX:MaxRAMPercentage=75.0",
                "-Dsun.net.client.defaultConnectTimeout=5000",
                "-Dsun.net.client.defaultReadTimeout=10000",
                "-Dfml.queryResult=confirm",
                "-jar", "minecraft-1.20.1.jar", "nogui",
            ],
        )

    def test_runtime_auto_installs_forge_then_launches_generated_arguments(self) -> None:
        server = self.root / "server"
        binaries = self.root / "bin"
        server.mkdir()
        binaries.mkdir()
        with zipfile.ZipFile(server / "forge-installer.jar", "w") as archive:
            archive.writestr("install_profile.json", "{}")
        java = binaries / "java"
        java.write_text(
            "#!/usr/bin/env bash\n"
            "if [[ \"$*\" == *--installServer* ]]; then\n"
            "  mkdir -p \"$MC_SERVER_DIR/libraries/forge/test\"\n"
            "  : > \"$MC_SERVER_DIR/libraries/forge/test/unix_args.txt\"\n"
            "else\n"
            "  printf '%s\\n' \"$@\" > \"$MC_SERVER_DIR/java-args.txt\"\n"
            "fi\n",
            encoding="utf-8",
        )
        java.chmod(0o755)
        environment = os.environ.copy()
        environment.update({
            "PATH": f"{binaries}:{environment['PATH']}",
            "MC_SERVER_DIR": str(server),
            "MC_CONSOLE_PIPE": str(self.root / "console.pipe"),
        })

        result = subprocess.run(
            ["bash", str(PROJECT_ROOT / "manager/runtime/entrypoint.sh"), "mc-auto"],
            env=environment, check=True, capture_output=True, text=True,
        )

        self.assertIn("检测到服务端安装器", result.stdout)
        self.assertEqual(
            (server / "java-args.txt").read_text(encoding="utf-8").splitlines(),
            [
                "-XX:InitialRAMPercentage=25.0", "-XX:MaxRAMPercentage=75.0",
                "-Dsun.net.client.defaultConnectTimeout=5000",
                "-Dsun.net.client.defaultReadTimeout=10000",
                "-Dfml.queryResult=confirm",
                "@libraries/forge/test/unix_args.txt", "nogui",
            ],
        )

    def test_force_create_replaces_existing_pack(self) -> None:
        shutil.copytree(PROJECT_ROOT / "manager/modpacks/template", self.packs / "template")
        manager = ModpackManager(self.root)
        target = manager.create("replace-me")
        (target / "src/old.txt").write_text("old", encoding="utf-8")

        replaced = manager.create("replace-me", force=True)

        self.assertEqual(replaced, target)
        self.assertFalse((target / "src/old.txt").exists())
        self.assertTrue((target / "config.yml").is_file())
        self.assertEqual(list((self.packs / ".staging").iterdir()), [])

    def test_force_create_refuses_running_pack(self) -> None:
        shutil.copytree(PROJECT_ROOT / "manager/modpacks/template", self.packs / "template")
        manager = ModpackManager(self.root)
        target = manager.create("running-pack")
        manager.docker.running = Mock(return_value=True)

        with self.assertRaisesRegex(ModpackError, "正在运行"):
            manager.create("running-pack", force=True)

        self.assertTrue((target / "config.yml").is_file())

    def test_failed_force_create_keeps_existing_pack(self) -> None:
        shutil.copytree(PROJECT_ROOT / "manager/modpacks/template", self.packs / "template")
        manager = ModpackManager(self.root)
        target = manager.create("keep-me")
        marker = target / "src/keep.txt"
        marker.write_text("keep", encoding="utf-8")

        with patch(
            "manager.src.modpacks.shutil.copytree",
            side_effect=OSError("copy failed"),
        ), self.assertRaisesRegex(OSError, "copy failed"):
            manager.create("keep-me", force=True)

        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")
        self.assertEqual(list((self.packs / ".staging").iterdir()), [])

    def test_export_import_contract_preserves_runtime_data(self) -> None:
        source = self.make_pack("source")
        archive = export_pack(
            "source", self.packs, mode="source-only",
            destination=self.root / "source.tgz",
        )
        destination_root = self.root / "destination"
        destination_root.mkdir()
        shutil.copytree(PROJECT_ROOT / "manager/runtime", destination_root / "runtime")
        destination = ModpackManager(destination_root)
        destination.docker.running = Mock(return_value=False)

        deployed = import_pack(destination, "source", archive, force=False)
        (deployed / "data/world.dat").parent.mkdir(exist_ok=True)
        (deployed / "data/world.dat").write_text("keep", encoding="utf-8")
        (source / "src/new.txt").write_text("updated", encoding="utf-8")
        replacement = export_pack(
            "source", self.packs, mode="source-only",
            destination=self.root / "replacement.tgz",
        )
        import_pack(destination, "source", replacement, force=True)

        self.assertEqual((deployed / "src/new.txt").read_text(), "updated")
        self.assertEqual((deployed / "data/world.dat").read_text(), "keep")

    def test_import_rejects_unsafe_archive_member(self) -> None:
        archive = self.root / "unsafe.tar.gz"
        payload = self.root / "payload"
        payload.write_text("bad", encoding="utf-8")
        import tarfile
        with tarfile.open(archive, "w:gz") as output:
            output.add(payload, arcname="../escape")
        with self.assertRaisesRegex(RuntimeError, "不安全路径"):
            import_pack(ModpackManager(self.root), "target", archive, force=False)


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict]] = []
        self.running = False
        self.image_exists = False

    def __call__(self, arguments: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        self.calls.append((arguments, kwargs))
        output, error, code = "", "", 0
        if arguments[:3] == ["docker", "image", "inspect"]:
            code = 0 if self.image_exists else 1
        elif "--status" in arguments and "running" in arguments:
            output = "container-id\n" if self.running else ""
        elif arguments[:2] == ["docker", "inspect"]:
            output = "running|healthy|0\n" if ".State.Status" in " ".join(arguments) else "0\n"
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

    def test_refuses_root_or_invalid_container_user(self) -> None:
        for uid, message in (("0", "root"), ("00", "root"), ("abc", "整数")):
            with self.subTest(uid=uid), patch.dict(
                os.environ,
                {"MC_CONTAINER_UID": uid, "MC_CONTAINER_GID": "1000"},
            ), self.assertRaisesRegex(DockerError, message):
                self.client._environment()

    def test_start_builds_missing_image_then_waits_for_health(self) -> None:
        self.client.start(self.spec)
        commands = [call[0] for call in self.runner.calls]
        self.assertTrue(any(command[:2] == ["docker", "build"] for command in commands))
        up = next(command for command in commands if "up" in command)
        self.assertEqual(up[-2:], ["up", "-d"])
        self.assertTrue(any(".State.Health" in " ".join(command) for command in commands))

    def test_start_is_idempotent_when_already_running(self) -> None:
        self.runner.running = True
        self.client.start(self.spec)
        self.assertFalse(any("build" in command or "up" in command for command, _ in self.runner.calls))
        self.assertTrue(any(
            ".State.Health" in " ".join(command)
            for command, _ in self.runner.calls
        ))

    def test_existing_runtime_image_is_reused_without_building(self) -> None:
        self.runner.image_exists = True

        self.client.ensure_image(17)

        commands = [command for command, _ in self.runner.calls]
        self.assertTrue(any(command[:3] == ["docker", "image", "inspect"] for command in commands))
        self.assertFalse(any(command[:2] == ["docker", "build"] for command in commands))

    def test_console_passes_user_text_only_through_stdin(self) -> None:
        self.runner.running = True
        text = "say hello; stop"
        self.client.console(self.spec, text)
        command, options = self.runner.calls[-1]
        self.assertNotIn(text, command)
        self.assertEqual(options["input"], text + "\n")

    def test_stop_checks_for_clean_container_exit(self) -> None:
        self.runner.running = True
        events = []
        self.client.events = lambda stage, current, total, message, detail: events.append(message)
        self.client.stop(self.spec)
        commands = [call[0] for call in self.runner.calls]
        self.assertTrue(any("stop" in command for command in commands))
        self.assertTrue(any(command[:2] == ["docker", "inspect"] for command in commands))
        self.assertTrue(any("发送停止信号" in message for message in events))
        self.assertTrue(any("安全退出" in message for message in events))


if __name__ == "__main__":
    unittest.main()
