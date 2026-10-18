from __future__ import annotations

import subprocess
import tempfile
import unittest
import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from lib.config import Endpoint
from lib.deploy import (
    DockerProvisioner, PythonProvisioner, RuntimeDeployment,
)
from lib.remote import CommandError, _REMOTE_MANIFEST, _ignored, run_logged


class FakeTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str], dict]] = []
        self.uploads: list[tuple[str, bytes]] = []
        self.syncs: list[tuple[Path, str, str, tuple[str, ...]]] = []

    def ssh(self, destination: str, arguments: list[str], **options):
        self.calls.append((destination, arguments, options))
        return subprocess.CompletedProcess(arguments, 0, stdout=b"")

    def ssh_logged(self, destination: str, arguments: list[str], **options):
        self.calls.append((destination, arguments, options))
        return subprocess.CompletedProcess(arguments, 0, stdout=b"")

    def ssh_tail(self, destination: str, arguments: list[str], **options):
        self.calls.append((destination, arguments, options))
        return subprocess.CompletedProcess(arguments, 0, stdout=b"")

    def scp(self, source: Path, destination: str) -> None:
        self.uploads.append((destination, source.read_bytes()))

    def upload_file(self, source: Path, destination: str, remote_path: str, *, mode=None):
        self.uploads.append((f"{destination}:{remote_path}", source.read_bytes()))
        return True

    def sync_tree(
        self, source: Path, destination: str, remote_root: str, *,
        preserve=(), exclude=(),
    ):
        self.syncs.append((source, destination, remote_root, tuple(preserve)))
        return 1, 0

class RuntimeDeploymentTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "runtime"
        self.source.mkdir()
        (self.source / "runtime.sh").write_text(
            "#!/usr/bin/env bash\nexit 0\n", encoding="utf-8",
        )
        self.transport = FakeTransport()
        self.deployment = RuntimeDeployment(
            self.source,
            Endpoint("runtime.example", "mcsp", "/srv/mcsp/runtime"),
            "runtime.sh",
            transport=self.transport,
        )

    def scripts(self) -> str:
        return "\n".join(
            str(options.get("input_text", "")) + " " + " ".join(arguments)
            for _, arguments, options in self.transport.calls
        )

    def test_check_only_checks_local_entrypoint_ssh_and_runtime_requirements(self) -> None:
        self.deployment.check()
        self.assertFalse(self.transport.uploads)
        self.assertIn("bash -s -- check", self.scripts())

    def test_docker_provisioner_does_not_use_runtime_directory_or_source(self) -> None:
        DockerProvisioner(
            "runtime.example", "mcsp",
            transport=self.transport,
        ).install()
        self.assertFalse(self.transport.uploads)
        destination, arguments, options = self.transport.calls[-1]
        self.assertEqual(destination, "mcsp@runtime.example")
        self.assertEqual(arguments, ["bash", "-s", "--"])
        self.assertIn("download.docker.com", options["input_text"])
        self.assertIn("docker.m.daocloud.io", options["input_text"])
        self.assertIn("os.replace(temporary, path)", options["input_text"])
        self.assertIn("DEBIAN_FRONTEND=noninteractive", options["input_text"])
        self.assertIn("--retry-all-errors", options["input_text"])
        self.assertNotIn("/missing/runtime", options["input_text"])
        subprocess.run(
            ["bash", "-n"], input=options["input_text"], text=True, check=True,
        )

    def test_python_provisioner_installs_system_and_venv_dependencies(self) -> None:
        requirements = self.root / "requirements.txt"
        requirements.write_text("PyYAML==6.0.2\n", encoding="utf-8")
        PythonProvisioner(
            Endpoint("runtime.example", "mcsp", "/srv/manager"),
            requirements, transport=self.transport,
        ).install()
        self.assertEqual(len(self.transport.uploads), 1)
        destination, content = self.transport.uploads[0]
        self.assertEqual(
            destination,
            "mcsp@runtime.example:/srv/manager/.mcsp-requirements.txt",
        )
        self.assertEqual(content, b"PyYAML==6.0.2\n")
        scripts = self.scripts()
        self.assertIn("python3-pip python3-venv", scripts)
        self.assertIn('python3 -m venv "$root/.venv"', scripts)
        self.assertIn("pip install --requirement", scripts)
        self.assertNotIn("manager.sh", scripts)

    def test_logged_command_collapses_success_and_keeps_full_log(self) -> None:
        log = self.root / "terminal.log"
        output = io.StringIO()
        with patch("lib.remote.visible_tail_lines", return_value=10), redirect_stdout(output):
            run_logged(
                ["bash", "-c", "for i in {1..15}; do echo line-$i; done"],
                input_data=None, log_path=log, label="测试部署",
            )
        rendered = output.getvalue()
        self.assertNotIn("line-1\n", rendered)
        self.assertIn("测试部署：line-15", rendered)
        detail = log.read_text(encoding="utf-8")
        self.assertIn("line-1\n", detail)
        self.assertIn("line-15\n", detail)

    def test_logged_command_prints_only_error_tail_on_failure(self) -> None:
        log = self.root / "terminal.log"
        output = io.StringIO()
        error = io.StringIO()
        with (
            patch("lib.remote.visible_tail_lines", return_value=10),
            redirect_stdout(output), redirect_stderr(error),
            self.assertRaises(CommandError),
        ):
            run_logged(
                [
                    "bash", "-c",
                    "echo '[Python 2/3] 创建环境'; "
                    "for i in {1..15}; do echo line-$i; done; exit 7",
                ],
                input_data=None, log_path=log, label="测试失败",
            )
        lines = output.getvalue().splitlines()
        self.assertEqual(lines.count("[Python 2/3] 创建环境"), 1)
        self.assertNotIn("line-5", lines)
        self.assertIn("line-6", lines)
        self.assertIn("line-15", lines)
        self.assertIn(str(log), error.getvalue())

    def test_install_uploads_release_and_invokes_public_install(self) -> None:
        self.deployment.install("value: 1\n", "TOKEN=secret\n")
        destinations = [item[0] for item in self.transport.uploads]
        self.assertEqual(len(self.transport.syncs), 1)
        self.assertTrue(any(item.endswith("/config.yml") for item in destinations))
        self.assertTrue(any(item.endswith("/.env") for item in destinations))
        self.assertIn("./runtime.sh install", self.scripts())
        self.assertNotIn("apt-get", self.scripts())

    def test_publish_bootstraps_code_without_remote_requirement_check(self) -> None:
        self.deployment.publish()
        self.assertEqual(len(self.transport.syncs), 1)
        self.assertFalse(self.transport.uploads)
        self.assertNotIn("bash -s -- check", self.scripts())
        self.assertNotIn("./runtime.sh install", self.scripts())

    def test_configure_does_not_upload_a_release(self) -> None:
        self.deployment.configure("value: 2\n")
        self.assertEqual(len(self.transport.uploads), 1)
        self.assertTrue(self.transport.uploads[0][0].endswith("/config.yml"))
        self.assertIn("./runtime.sh apply-config", self.scripts())

    def test_sync_ignore_supports_template_negation(self) -> None:
        patterns = ("modpacks/*", "!modpacks/template", "!modpacks/template/*")
        self.assertTrue(_ignored("modpacks/demo/server.jar", patterns))
        self.assertFalse(_ignored("modpacks/template/config.yml", patterns))

    def test_remote_manifest_only_enters_explicit_preserved_subtrees(self) -> None:
        remote_root = self.root / "remote-manifest"
        (remote_root / "modpacks/template").mkdir(parents=True)
        (remote_root / "modpacks/live").mkdir(parents=True)
        (remote_root / "modpacks/template/current.txt").write_text("template")
        (remote_root / "modpacks/live/world.dat").write_text("world")
        (remote_root / "obsolete.py").write_text("old")
        result = subprocess.run(
            [
                "python3", "-c", _REMOTE_MANIFEST, str(remote_root),
                json.dumps(["modpacks"]), json.dumps(["modpacks/template"]),
            ],
            capture_output=True, text=True, check=True,
        )
        manifest = json.loads(result.stdout)
        self.assertIn("obsolete.py", manifest["files"])
        self.assertIn("modpacks/template/current.txt", manifest["files"])
        self.assertNotIn("modpacks/live/world.dat", manifest["files"])

    def test_preserved_paths_are_explicit_constructor_data(self) -> None:
        deployment = RuntimeDeployment(
            self.source,
            Endpoint("runtime.example", "mcsp", "/srv/mcsp/runtime"),
            "runtime.sh", preserve=("data",), transport=self.transport,
        )
        deployment.install("value: 1\n", "")
        self.assertEqual(
            self.transport.syncs[-1][3], ("config.yml", ".env", "data"),
        )

    def test_preserved_paths_must_be_root_names(self) -> None:
        for name in ("", ".", "..", "data/world", "data\\world"):
            with self.subTest(name=name), self.assertRaisesRegex(
                RuntimeError, "根目录",
            ):
                RuntimeDeployment(
                    self.source,
                    Endpoint("runtime.example", "mcsp", "/srv/mcsp/runtime"),
                    "runtime.sh", preserve=(name,), transport=self.transport,
                )

if __name__ == "__main__":
    unittest.main()
