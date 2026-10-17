from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from manager.lib.config import (
    ConfigError, load_pack, normalize_persistent, parse_interval, render_compose,
    validate_pack_id,
)
from manager.lib.docker import DockerClient, DockerInstaller
from manager.lib.modpacks import ModpackError, ModpackManager, _read_url
from manager.lib.transfer import export_pack, import_pack


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

    def test_create_vanilla_downloads_verified_official_server(self) -> None:
        shutil.copytree(PROJECT_ROOT / "manager/modpacks/template", self.packs / "template")
        server = b"official-server-jar"
        manifest_url = (
            "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
        )
        version_url = "https://piston-meta.mojang.com/v1/packages/test/version.json"
        server_url = "https://piston-data.mojang.com/v1/objects/test/server.jar"
        responses = {
            manifest_url: json.dumps({
                "latest": {"release": "1.21.10"},
                "versions": [{"id": "1.21.10", "url": version_url}],
            }).encode(),
            version_url: json.dumps({
                "javaVersion": {"majorVersion": 21},
                "downloads": {"server": {
                    "url": server_url, "size": len(server),
                    "sha1": hashlib.sha1(server).hexdigest(),
                }},
            }).encode(),
            server_url: server,
        }

        class Response:
            def __init__(self, content: bytes) -> None:
                self.content = content

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return None

            def read(self, limit: int) -> bytes:
                return self.content[:limit]

        def open_url(url: str, *, timeout: int):
            self.assertEqual(timeout, 60)
            return Response(responses[url])

        manager = ModpackManager(self.root)
        with patch("manager.lib.modpacks.urlopen", side_effect=open_url):
            target = manager.create(
                "vanilla", vanilla="latest", accept_eula=True,
            )

        self.assertEqual((target / "src/server.jar").read_bytes(), server)
        self.assertEqual((target / "src/eula.txt").read_text(), "eula=true\n")
        self.assertTrue((target / "src/user_jvm_args.txt").is_file())
        config = yaml.safe_load((target / "config.yml").read_text())
        self.assertEqual(config["version"], "1.21.10")
        self.assertEqual(config["java"], 21)
        self.assertEqual(config["command"], [
            "java", "-jar", "server.jar", "nogui",
        ])

    def test_runtime_entrypoint_injects_editable_jvm_arguments(self) -> None:
        server = self.root / "server"
        binaries = self.root / "bin"
        server.mkdir()
        binaries.mkdir()
        (server / "user_jvm_args.txt").write_text(
            "# custom arguments\n-Xms2G\r\n\n-Dserver.name=My Server\n",
            encoding="utf-8",
        )
        java = binaries / "java"
        java.write_text(
            "#!/usr/bin/env bash\nprintf '%s\\n' \"$@\" > java-args.txt\n",
            encoding="utf-8",
        )
        java.chmod(0o755)
        environment = os.environ.copy()
        environment.update({
            "PATH": f"{binaries}:{environment['PATH']}",
            "MC_SERVER_DIR": str(server),
            "MC_CONSOLE_PIPE": str(self.root / "console.pipe"),
        })

        subprocess.run([
            "bash", str(PROJECT_ROOT / "manager/runtime/entrypoint.sh"),
            "java", "-jar", "server.jar", "nogui",
        ], env=environment, check=True, capture_output=True, text=True)

        self.assertEqual(
            (server / "java-args.txt").read_text(encoding="utf-8").splitlines(),
            ["-Xms2G", "-Dserver.name=My Server", "-jar", "server.jar", "nogui"],
        )

    def test_download_falls_back_to_verified_curl(self) -> None:
        result = subprocess.CompletedProcess(
            ["curl"], 0, stdout=b"downloaded", stderr=b"",
        )
        with patch("manager.lib.modpacks.urlopen", side_effect=OSError("TLS EOF")), \
             patch("manager.lib.modpacks.time.sleep"), \
             patch("manager.lib.modpacks.shutil.which", return_value="/usr/bin/curl"), \
             patch("manager.lib.modpacks.subprocess.run", return_value=result) as run:
            self.assertEqual(_read_url("https://example.test/file", limit=20), b"downloaded")

        arguments = run.call_args.args[0]
        self.assertIn("--ipv4", arguments)
        self.assertIn("--http1.1", arguments)
        self.assertIn("--max-filesize", arguments)
        self.assertNotIn("--insecure", arguments)

    def test_default_jvm_arguments_match_template_memory_limit(self) -> None:
        arguments = (
            PROJECT_ROOT / "manager/modpacks/template/src/user_jvm_args.txt"
        ).read_text(encoding="utf-8")
        self.assertIn("-Xms2G\n", arguments)
        self.assertIn("-Xmx6G\n", arguments)
        self.assertIn("-XX:+UseG1GC\n", arguments)
        self.assertNotIn("Dfml", arguments)

    def test_accept_eula_requires_vanilla_and_leaves_no_partial_pack(self) -> None:
        shutil.copytree(PROJECT_ROOT / "manager/modpacks/template", self.packs / "template")
        manager = ModpackManager(self.root)
        with self.assertRaisesRegex(ModpackError, "只能与 --vanilla"):
            manager.create("invalid", accept_eula=True)
        self.assertFalse((self.packs / "invalid").exists())

    def test_force_create_replaces_existing_pack(self) -> None:
        shutil.copytree(PROJECT_ROOT / "manager/modpacks/template", self.packs / "template")
        manager = ModpackManager(self.root)
        target = manager.create("replace-me")
        (target / "src/old.txt").write_text("old", encoding="utf-8")

        replaced = manager.create("replace-me", force=True)

        self.assertEqual(replaced, target)
        self.assertFalse((target / "src/old.txt").exists())
        self.assertTrue((target / "config.yml").is_file())
        self.assertEqual(list((self.root / "tmp").iterdir()), [])

    def test_failed_force_create_keeps_existing_pack(self) -> None:
        shutil.copytree(PROJECT_ROOT / "manager/modpacks/template", self.packs / "template")
        manager = ModpackManager(self.root)
        target = manager.create("keep-me")
        marker = target / "src/keep.txt"
        marker.write_text("keep", encoding="utf-8")

        with patch(
            "manager.lib.modpacks.install_vanilla",
            side_effect=ModpackError("download failed"),
        ), self.assertRaisesRegex(ModpackError, "download failed"):
            manager.create("keep-me", vanilla="latest", force=True)

        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")
        self.assertEqual(list((self.root / "tmp").iterdir()), [])

    def test_export_import_contract_preserves_runtime_data(self) -> None:
        source = self.make_pack("source")
        archive = export_pack("source", self.packs)
        destination_root = self.root / "destination"
        destination_root.mkdir()
        shutil.copytree(PROJECT_ROOT / "manager/runtime", destination_root / "runtime")
        destination = ModpackManager(destination_root)

        deployed = import_pack(destination, "target", archive, force=False)
        (deployed / "data/world.dat").parent.mkdir(exist_ok=True)
        (deployed / "data/world.dat").write_text("keep", encoding="utf-8")
        (source / "src/new.txt").write_text("updated", encoding="utf-8")
        replacement = export_pack("source", self.packs)
        import_pack(destination, "target", replacement, force=True)

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


class DockerInstallRunner:
    def __init__(self, *, installed: bool = False) -> None:
        self.installed = installed
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, arguments: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        self.calls.append((arguments, kwargs))
        output, error, code = "", "", 0
        if arguments == ["dpkg", "--print-architecture"]:
            output = "amd64\n"
        elif arguments[:2] == ["dpkg-query", "-W"]:
            code = 1
        elif arguments[:2] == ["docker", "--version"]:
            code = 0 if self.installed else 1
        elif arguments[:3] == ["docker", "compose", "version"]:
            code = 0 if self.installed else 1
        elif arguments[:4] == ["apt-get", "install", "-y", "docker-ce"]:
            self.installed = True
        return subprocess.CompletedProcess(arguments, code, output, error)


class DockerInstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.os_release = Path(temporary.name) / "os-release"
        self.os_release.write_text(
            'ID=debian\nVERSION_CODENAME="bookworm"\n', encoding="utf-8",
        )

    def test_installs_from_official_repository_and_verifies(self) -> None:
        runner = DockerInstallRunner()
        result = DockerInstaller(
            runner, os_release=self.os_release, effective_uid=0,
            invoking_user="root",
        ).install()
        commands = [arguments for arguments, _ in runner.calls]
        self.assertFalse(result.already_installed)
        self.assertIn([
            "curl", "-fsSL", "https://download.docker.com/linux/debian/gpg",
            "-o", "/etc/apt/keyrings/docker.asc",
        ], commands)
        install = next(
            arguments for arguments in commands
            if arguments[:4] == ["apt-get", "install", "-y", "docker-ce"]
        )
        self.assertIn("docker-compose-plugin", install)
        tee_index = commands.index(["tee", "/etc/apt/sources.list.d/docker.sources"])
        source = runner.calls[tee_index][1]["input"]
        self.assertIn("Suites: bookworm", source)
        self.assertIn("Architectures: amd64", source)
        self.assertIn([
            "systemctl", "enable", "--now", "docker.service", "containerd.service",
        ], commands)
        self.assertIn(["docker", "info"], commands)

    def test_existing_install_is_kept_and_user_gets_access(self) -> None:
        runner = DockerInstallRunner(installed=True)
        result = DockerInstaller(
            runner, os_release=self.os_release, effective_uid=0,
            invoking_user="steam",
        ).install()
        commands = [arguments for arguments, _ in runner.calls]
        self.assertTrue(result.already_installed)
        self.assertEqual(result.user_added, "steam")
        self.assertIn(
            ["usermod", "--append", "--groups", "docker", "steam"], commands,
        )
        self.assertFalse(any("apt-get" in arguments for arguments in commands))

    def test_rejects_unsupported_distribution_before_mutation(self) -> None:
        self.os_release.write_text("ID=arch\nVERSION_CODENAME=rolling\n")
        runner = DockerInstallRunner()
        with self.assertRaisesRegex(RuntimeError, "Debian 或 Ubuntu"):
            DockerInstaller(
                runner, os_release=self.os_release, effective_uid=0,
                invoking_user="root",
            ).install()
        commands = [arguments for arguments, _ in runner.calls]
        self.assertFalse(any("apt-get" in arguments for arguments in commands))


if __name__ == "__main__":
    unittest.main()
