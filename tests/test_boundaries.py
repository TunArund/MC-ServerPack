from __future__ import annotations

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RuntimeBoundaryTests(unittest.TestCase):
    def test_terminal_cli_only_calls_runtime_facades(self) -> None:
        source = (ROOT / "lib/cli.py").read_text(encoding="utf-8")
        tree = ast.parse(source, filename="lib/cli.py")
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
        shared = (ROOT / "lib/deploy.py").read_text(encoding="utf-8")
        docker = (ROOT / "lib/docker.py").read_text(encoding="utf-8")
        docker_script = (ROOT / "lib/docker-install.sh").read_text(encoding="utf-8")
        tree = ast.parse(shared, filename="lib/deploy.py")
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
        self.assertIn("class PythonProvisioner", shared)
        self.assertIn("python3-venv", shared)
        self.assertNotIn("docker", imports)
        self.assertTrue((ROOT / "lib/manager.py").is_file())
        self.assertTrue((ROOT / "lib/monitor.py").is_file())
        self.assertFalse((ROOT / "lib/manager/service.py").exists())
        self.assertFalse((ROOT / "lib/monitor/service.py").exists())

    def test_only_terminal_installs_docker(self) -> None:
        terminal = (ROOT / "lib/docker.py").read_text(encoding="utf-8")
        installer = (ROOT / "lib/docker-install.sh").read_text(encoding="utf-8")
        manager = (ROOT / "manager/lib/docker.py").read_text(encoding="utf-8")
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
        self.assertEqual(expected, (ROOT / "manager/lib/power.py").read_bytes())
        self.assertEqual(expected, (ROOT / "monitor/src/power.py").read_bytes())

        self.assertNotIn(b"load_adapter", expected)

    def test_shared_provider_implementations_are_identical(self) -> None:
        for name in ("tencent.py", "example.py"):
            expected = (ROOT / "lib/providers" / name).read_bytes()
            with self.subTest(name=name, runtime="manager"):
                self.assertEqual(expected, (ROOT / "manager/lib/providers" / name).read_bytes())
            with self.subTest(name=name, runtime="monitor"):
                self.assertEqual(expected, (ROOT / "monitor/src/providers" / name).read_bytes())

        tencent = (ROOT / "lib/providers/tencent.py").read_text(encoding="utf-8")
        self.assertIn("def _request(", tencent)
        self.assertNotIn("def _invoke(", tencent)

    def test_deploy_metadata_and_transport_have_single_owners(self) -> None:
        remote = (ROOT / "lib/remote.py").read_text(encoding="utf-8")
        self.assertNotIn("def archive_tree", remote)
        self.assertNotIn("def cleanup_archive", remote)
        self.assertFalse((ROOT / "manager/.mcsp-preserve").exists())
        self.assertFalse((ROOT / "monitor/.mcsp-preserve").exists())
        self.assertTrue((ROOT / "manager/.mcspignore").is_file())
        self.assertTrue((ROOT / "monitor/.mcspignore").is_file())

    def test_manager_module_names_describe_both_directions(self) -> None:
        self.assertTrue((ROOT / "manager/lib/backup.py").is_file())
        self.assertFalse((ROOT / "manager/lib/archive.py").exists())
        transfer = (ROOT / "manager/lib/transfer.py").read_text(encoding="utf-8")
        self.assertIn("def export_pack", transfer)
        self.assertIn("def import_pack", transfer)

    def test_no_cross_runtime_imports(self) -> None:
        forbidden = {
            "lib": ("manager", "monitor"),
            "manager/lib": ("monitor",),
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
                "app.py", "config.py", "network.py", "power.py", "qqbot.py",
                "schedule.py",
            ],
        )
        self.assertFalse((ROOT / "monitor/src/monitor").exists())
        self.assertFalse((ROOT / "monitor/src/qqbot").exists())
        self.assertFalse((ROOT / "monitor/lib").exists())

    def test_monitor_domain_does_not_import_global_config(self) -> None:
        for path in (ROOT / "monitor/src").glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    self.assertLess(node.level, 2, f"{path}: global config must be injected")

    def test_provider_implementations_do_not_import_power_state_machine(self) -> None:
        for directory in (
            ROOT / "lib/providers", ROOT / "manager/lib/providers",
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
