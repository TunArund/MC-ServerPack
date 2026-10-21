from __future__ import annotations

import ast
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class RuntimeBoundaryTests(unittest.TestCase):
    def test_terminal_has_an_independent_runtime_root(self) -> None:
        terminal = ROOT / "terminal"
        for name in (
            "mcsp.sh", "config.yml.example", ".env.example",
            "requirements.txt", "readme.md", "src", "logs",
        ):
            with self.subTest(name=name):
                self.assertTrue((terminal / name).exists())
        self.assertFalse((ROOT / "mcsp.sh").exists())
        self.assertFalse((ROOT / "config.yml.example").exists())

    def test_terminal_entrypoint_runs_with_only_terminal_on_python_path(self) -> None:
        result = subprocess.run(
            ["bash", str(ROOT / "terminal/mcsp.sh"), "--help"],
            cwd=ROOT / "terminal", capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Minecraft 服务端部署与管理", result.stdout)

    def test_terminal_cli_only_calls_runtime_facades(self) -> None:
        source = (ROOT / "terminal/src/cli.py").read_text(encoding="utf-8")
        tree = ast.parse(source, filename="terminal/src/cli.py")
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported.update(alias.name for alias in node.names)
        self.assertTrue({"Manager", "Monitor"} <= imported)
        for internal in (
            "create_power", "load_env", "upsert_user_target",
            "RuntimeDeployment", "ModpackManager",
        ):
            self.assertNotIn(internal, imported)
        self.assertNotIn("manager.sh", source)
        self.assertNotIn("monitor.sh", source)

    def test_terminal_runtime_adapters_are_flat_modules(self) -> None:
        shared = (ROOT / "terminal/src/deploy.py").read_text(encoding="utf-8")
        docker = (ROOT / "terminal/src/docker.py").read_text(encoding="utf-8")
        docker_script = (ROOT / "terminal/src/docker-install.sh").read_text(encoding="utf-8")
        tree = ast.parse(shared, filename="terminal/src/deploy.py")
        imports = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        self.assertNotIn("class DockerProvisioner", shared)
        self.assertIn("class DockerProvisioner", docker)
        self.assertIn('resource("docker-install.sh")', docker)
        self.assertNotIn("apt-get", docker)
        self.assertIn("apt-get", docker_script)
        self.assertNotIn("class PythonProvisioner", shared)
        self.assertNotIn("python3-venv", shared)
        self.assertNotIn("docker", imports)
        self.assertTrue((ROOT / "terminal/src/manager.py").is_file())
        self.assertTrue((ROOT / "terminal/src/monitor.py").is_file())
        self.assertFalse((ROOT / "terminal/manager/service.py").exists())
        self.assertFalse((ROOT / "terminal/monitor/service.py").exists())

    def test_only_terminal_installs_docker(self) -> None:
        terminal = (ROOT / "terminal/src/docker.py").read_text(encoding="utf-8")
        installer = (ROOT / "terminal/src/docker-install.sh").read_text(encoding="utf-8")
        manager = (ROOT / "lib/docker_sdk.py").read_text(encoding="utf-8")
        manager_entry = (ROOT / "manager/manager.sh").read_text(encoding="utf-8")
        monitor_entry = (ROOT / "monitor/monitor.sh").read_text(encoding="utf-8")
        self.assertIn("DockerProvisioner", terminal)
        self.assertIn("apt-get", installer)
        self.assertNotIn("apt-get", terminal)
        for source in (manager, manager_entry, monitor_entry):
            self.assertNotIn("apt-get", source)
            self.assertNotIn("download.docker.com", source)
        self.assertNotIn("install docker", manager_entry)

    def test_power_core_is_identical_in_all_runtimes(self) -> None:
        expected = (ROOT / "lib/power.py").read_bytes()
        self.assertEqual(expected, (ROOT / "terminal/src/power.py").read_bytes())
        self.assertEqual(expected, (ROOT / "manager/src/power.py").read_bytes())
        self.assertEqual(expected, (ROOT / "monitor/src/power.py").read_bytes())

        self.assertNotIn(b"load_adapter", expected)

    def test_manager_sdk_is_identical_in_all_runtimes(self) -> None:
        expected = (ROOT / "manager/src/manager_sdk.py").read_bytes()
        self.assertEqual(expected, (ROOT / "lib/manager_sdk.py").read_bytes())
        self.assertEqual(expected, (ROOT / "terminal/src/manager_sdk.py").read_bytes())
        self.assertEqual(expected, (ROOT / "monitor/src/manager_sdk.py").read_bytes())
        self.assertNotIn(b"import manager", expected)
        self.assertNotIn(b"from manager", expected)

    def test_observability_is_identical_in_all_runtimes(self) -> None:
        expected = (ROOT / "lib/observability.py").read_bytes()
        self.assertEqual(expected, (ROOT / "terminal/src/observability.py").read_bytes())
        self.assertEqual(expected, (ROOT / "manager/src/observability.py").read_bytes())
        self.assertEqual(expected, (ROOT / "monitor/src/observability.py").read_bytes())

    def test_shared_provider_implementations_are_identical(self) -> None:
        for name in ("tencent.py", "example.py"):
            expected = (ROOT / "lib/providers" / name).read_bytes()
            with self.subTest(name=name, runtime="terminal"):
                self.assertEqual(expected, (ROOT / "terminal/src/providers" / name).read_bytes())
            with self.subTest(name=name, runtime="manager"):
                self.assertEqual(expected, (ROOT / "manager/src/providers" / name).read_bytes())
            with self.subTest(name=name, runtime="monitor"):
                self.assertEqual(expected, (ROOT / "monitor/src/providers" / name).read_bytes())

        tencent = (ROOT / "lib/providers/tencent.py").read_text(encoding="utf-8")
        self.assertIn("def _request(", tencent)
        self.assertNotIn("def _invoke(", tencent)

    def test_terminal_has_no_repo_root_runtime_imports(self) -> None:
        for path in (ROOT / "terminal/src").rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            with self.subTest(path=path):
                self.assertNotIn("from lib.", source)
                self.assertNotIn("import lib.", source)

    def test_deploy_metadata_and_transport_have_single_owners(self) -> None:
        remote = (ROOT / "terminal/src/remote.py").read_text(encoding="utf-8")
        self.assertNotIn("def archive_tree", remote)
        self.assertNotIn("def cleanup_archive", remote)
        self.assertFalse((ROOT / "manager/.mcsp-preserve").exists())
        self.assertFalse((ROOT / "monitor/.mcsp-preserve").exists())
        self.assertTrue((ROOT / "manager/.mcspignore").is_file())
        self.assertTrue((ROOT / "monitor/.mcspignore").is_file())

    def test_manager_module_names_describe_both_directions(self) -> None:
        self.assertTrue((ROOT / "manager/src/backup.py").is_file())
        self.assertFalse((ROOT / "manager/src/archive.py").exists())
        transfer = (ROOT / "manager/src/transfer.py").read_text(encoding="utf-8")
        self.assertIn("archive_members, export_pack, file_sha256, verify_extracted", transfer)
        self.assertIn("def import_pack", transfer)

    def test_manager_jobs_are_separate_from_routes_and_schedule(self) -> None:
        router = (ROOT / "manager/src/router.py").read_text(encoding="utf-8")
        jobs = (ROOT / "manager/src/jobs.py").read_text(encoding="utf-8")
        schedule = (ROOT / "manager/src/schedule.py").read_text(encoding="utf-8")
        self.assertIn("self.routes", router)
        self.assertNotIn("class JobStore", router)
        self.assertIn("class JobStore", jobs)
        self.assertNotIn("class JobStore", schedule)

    def test_no_cross_runtime_imports(self) -> None:
        forbidden = {
            "terminal": ("manager.src", "monitor.src"),
            "lib": ("terminal", "manager", "monitor"),
            "manager/src": ("monitor",),
            "monitor/src": ("manager",),
        }
        for directory, prefixes in forbidden.items():
            for path in (ROOT / directory).rglob("*.py"):
                with self.subTest(path=path):
                    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                    imports: list[str] = []
                    for node in ast.walk(tree):
                        if isinstance(node, ast.Import):
                            imports.extend(alias.name for alias in node.names)
                        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                            imports.append(node.module)
                    for name in imports:
                        self.assertFalse(name.startswith(prefixes), f"{path}: cross-runtime import {name}")

    def test_monitor_uses_one_flat_source_root(self) -> None:
        modules = sorted(path.name for path in (ROOT / "monitor/src").glob("*.py"))
        self.assertEqual(
            modules,
            [
                "app.py", "config.py", "docker_sdk.py", "manager_sdk.py", "message.py", "network.py",
                "observability.py", "power.py", "qqbot.py", "schedule.py", "targets.py",
            ],
        )
        self.assertFalse((ROOT / "monitor/src/monitor").exists())
        self.assertFalse((ROOT / "monitor/src/qqbot").exists())
        self.assertFalse((ROOT / "monitor/lib").exists())

    def test_runtime_containers_use_invoking_user(self) -> None:
        compose = (ROOT / "monitor/compose.yml").read_text(encoding="utf-8")
        self.assertIn("${MC_CONTAINER_UID:-1000}:${MC_CONTAINER_GID:-1000}", compose)
        script = (ROOT / "monitor/monitor.sh").read_text(encoding="utf-8")
        self.assertIn('export MC_CONTAINER_UID="$(id -u)"', script)
        self.assertIn('export MC_CONTAINER_GID="$(id -g)"', script)
        manager = (ROOT / "lib/docker_sdk.py").read_text(encoding="utf-8")
        manager_config = (ROOT / "manager/src/config.py").read_text(encoding="utf-8")
        self.assertIn("MC_CONTAINER_UID", manager)
        self.assertIn("拒绝以 root 运行", manager)
        self.assertIn(
            "${MC_CONTAINER_UID:-1000}:${MC_CONTAINER_GID:-1000}",
            manager_config,
        )

    def test_monitor_targets_are_dynamic_not_config_backed(self) -> None:
        forbidden = {
            "monitor/src/qqbot.py": "TargetPolicy",
            "monitor/src/config.py": "qqbot",
            "terminal/src/config.py": "upsert_monitor",
            "terminal/src/cli.py": "add-target",
        }
        for name, token in forbidden.items():
            with self.subTest(name=name):
                self.assertNotIn(token, (ROOT / name).read_text(encoding="utf-8"))
        self.assertNotIn(
            '"version"', (ROOT / "monitor/src/targets.py").read_text(encoding="utf-8"),
        )

    def test_monitor_domain_does_not_import_global_config(self) -> None:
        for path in (ROOT / "monitor/src").glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    self.assertLess(node.level, 2, f"{path}: global config must be injected")

    def test_provider_implementations_do_not_import_power_state_machine(self) -> None:
        for directory in (
            ROOT / "lib/providers", ROOT / "manager/src/providers",
            ROOT / "monitor/src/providers",
        ):
            for path in directory.glob("*.py"):
                with self.subTest(path=path):
                    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                    for node in ast.walk(tree):
                        if isinstance(node, ast.ImportFrom) and node.module:
                            self.assertFalse(
                                node.module.endswith("power"),
                                f"{path}: provider must not import power.py",
                            )


if __name__ == "__main__":
    unittest.main()
