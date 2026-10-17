from __future__ import annotations

import subprocess
import tarfile
import tempfile
import unittest
import io
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from lib.config import Endpoint
from lib.deploy import (
    DockerProvisioner, PythonProvisioner, RuntimeDeployment, archive_tree,
)
from lib.remote import CommandError, run_logged


class FakeTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str], dict]] = []
        self.uploads: list[tuple[str, bytes]] = []

    def ssh(self, destination: str, arguments: list[str], **options):
        self.calls.append((destination, arguments, options))
        return subprocess.CompletedProcess(arguments, 0, stdout=b"")

    def ssh_logged(self, destination: str, arguments: list[str], **options):
        self.calls.append((destination, arguments, options))
        return subprocess.CompletedProcess(arguments, 0, stdout=b"")

    def scp(self, source: Path, destination: str) -> None:
        self.uploads.append((destination, source.read_bytes()))

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
        log = self.root / "deploy.log"
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
        log = self.root / "deploy.log"
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
        self.assertTrue(any(item.endswith("/.mcsp-release.tar.gz") for item in destinations))
        self.assertTrue(any(item.endswith("/config.yml") for item in destinations))
        self.assertTrue(any(item.endswith("/.env") for item in destinations))
        self.assertIn("./runtime.sh install", self.scripts())
        self.assertNotIn("apt-get", self.scripts())

    def test_publish_bootstraps_code_without_remote_requirement_check(self) -> None:
        self.deployment.publish()
        destinations = [item[0] for item in self.transport.uploads]
        self.assertTrue(any(item.endswith("/.mcsp-release.tar.gz") for item in destinations))
        self.assertNotIn("bash -s -- check", self.scripts())
        self.assertNotIn("./runtime.sh install", self.scripts())

    def test_configure_does_not_upload_a_release(self) -> None:
        self.deployment.configure("value: 2\n")
        self.assertEqual(len(self.transport.uploads), 1)
        self.assertTrue(self.transport.uploads[0][0].endswith("/config.yml"))
        self.assertIn("./runtime.sh apply-config", self.scripts())

    def test_archive_honours_runtime_ignore_file(self) -> None:
        (self.source / ".mcspignore").write_text("private\n", encoding="utf-8")
        (self.source / "public").write_text("yes", encoding="utf-8")
        (self.source / "private").write_text("no", encoding="utf-8")
        archive = archive_tree(self.source)
        self.addCleanup(lambda: __import__("shutil").rmtree(archive.parent, ignore_errors=True))
        import tarfile
        with tarfile.open(archive, "r:gz") as handle:
            names = handle.getnames()
        self.assertIn("public", names)
        self.assertNotIn("private", names)

    def test_preserved_paths_are_explicit_constructor_data(self) -> None:
        deployment = RuntimeDeployment(
            self.source,
            Endpoint("runtime.example", "mcsp", "/srv/mcsp/runtime"),
            "runtime.sh", preserve=("data",), transport=self.transport,
        )
        deployment.install("value: 1\n", "")
        self.assertIn(" config.yml .env data ", self.scripts())
        self.assertNotIn(".mcsp-preserve", self.scripts())

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

    def test_extract_replaces_code_and_merges_preserved_directory(self) -> None:
        remote_root = self.root / "remote"
        (remote_root / "modpacks/live/data").mkdir(parents=True)
        (remote_root / "modpacks/live/data/world.dat").write_text("save")
        (remote_root / "config.yml").write_text("keep")
        (remote_root / "obsolete.py").write_text("remove")

        release = self.root / "release"
        (release / "modpacks/template").mkdir(parents=True)
        (release / "modpacks/template/config.yml").write_text("template")
        (release / "runtime.sh").write_text("new")
        archive = remote_root / ".mcsp-release.tar.gz"
        with tarfile.open(archive, "w:gz") as output:
            for path in sorted(release.rglob("*")):
                output.add(path, arcname=path.relative_to(release), recursive=False)

        deployment = RuntimeDeployment(
            self.source,
            Endpoint("runtime.example", "mcsp", str(remote_root)),
            "runtime.sh", preserve=("modpacks",), transport=self.transport,
        )
        deployment._extract(str(archive))
        script = self.transport.calls[-1][1][-1]
        subprocess.run(["bash", "-lc", script], check=True)

        self.assertEqual((remote_root / "config.yml").read_text(), "keep")
        self.assertEqual(
            (remote_root / "modpacks/live/data/world.dat").read_text(), "save",
        )
        self.assertEqual(
            (remote_root / "modpacks/template/config.yml").read_text(), "template",
        )
        self.assertEqual((remote_root / "runtime.sh").read_text(), "new")
        self.assertFalse((remote_root / "obsolete.py").exists())
        self.assertFalse(archive.exists())


if __name__ == "__main__":
    unittest.main()
