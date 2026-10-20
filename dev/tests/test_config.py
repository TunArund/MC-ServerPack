from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from terminal.src.config import (
    ConfigError, deployment_connection, deployment_endpoint, deployment_host,
    initialize, load, load_manager, load_monitor, render_runtime_section,
    runtime_section,
)


ROOT = Path(__file__).resolve().parents[2]


class ConfigTests(unittest.TestCase):
    def write(self, document: dict) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "config.yml"
        path.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")
        return path

    def minimum(self) -> dict:
        return {
            "manager": {
                "host": "manager.example",
                "user": "minecraft",
                "dir": "/srv/minecraft",
                "timezone": "Asia/Shanghai",
                "auto-stop": False,
                "modpack": {"java": [17, 21], "mem-limit": "16g"},
            },
            "monitor": {"enabled": False, "timezone": "Asia/Shanghai"},
            "cos": {"enabled": False},
        }

    def test_initialize_copies_examples_without_overwriting(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / "config.yml.example").write_text("manager: example\n")
        (root / ".env.example").write_text("TOKEN=\n")

        first = initialize(root / "config.yml")
        self.assertEqual(first, (
            (root / "config.yml", True),
            (root / ".env", True),
        ))
        self.assertEqual((root / "config.yml").read_text(), "manager: example\n")
        self.assertEqual((root / ".env").read_text(), "TOKEN=\n")
        self.assertEqual((root / ".env").stat().st_mode & 0o777, 0o600)

        (root / "config.yml").write_text("keep\n")
        second = initialize(root / "config.yml")
        self.assertEqual(second, (
            (root / "config.yml", False),
            (root / ".env", False),
        ))
        self.assertEqual((root / "config.yml").read_text(), "keep\n")

    def test_initialize_requires_both_examples_before_writing(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / "config.yml.example").write_text("manager: example\n")
        with self.assertRaisesRegex(ConfigError, "初始化模板"):
            initialize(root / "config.yml")
        self.assertFalse((root / "config.yml").exists())

    def test_minimum_disabled_optional_services(self) -> None:
        config = load(self.write(self.minimum()))
        self.assertEqual(config.manager.endpoint.host, "manager.example")
        self.assertEqual(config.manager.timezone, "Asia/Shanghai")
        self.assertEqual(config.manager.modpack.java, (17, 21))
        self.assertFalse(config.monitor.enabled)
        self.assertIsNone(config.monitor.endpoint)

    def test_rejects_invalid_runtime_timezone(self) -> None:
        document = self.minimum()
        document["manager"]["timezone"] = "Mars/Olympus"
        with self.assertRaisesRegex(ConfigError, "IANA 时区"):
            load_manager(self.write(document))

    def test_monitor_loading_does_not_validate_manager(self) -> None:
        document = self.minimum()
        document["manager"]["user"] = ""
        document["monitor"] = {
            "enabled": True,
            "host": "monitor.example",
            "user": "monitor",
            "dir": "/srv/monitor",
            "timezone": "Asia/Shanghai",
            "manager": {
                "power-api": {
                    "provider": "tencent",
                    "instance-id": "manager",
                    "instance-region": "ap-test",
                },
                "api": {"host": "manager.example", "port": 27016},
            },
        }
        config = load_monitor(self.write(document))
        self.assertEqual(config.endpoint.user, "monitor")

    def test_account_bootstrap_reads_only_target_host(self) -> None:
        document = self.minimum()
        document["manager"]["user"] = "root"
        document["manager"]["modpack"] = "not-yet-configured"
        self.assertEqual(
            deployment_host(self.write(document), "manager"),
            "manager.example",
        )

    def test_docker_bootstrap_reads_only_target_connection(self) -> None:
        document = self.minimum()
        document["manager"]["dir"] = "not-needed"
        document["manager"]["modpack"] = "not-yet-configured"
        self.assertEqual(
            deployment_connection(self.write(document), "manager"),
            ("manager.example", "minecraft"),
        )

    def test_python_bootstrap_reads_only_target_endpoint(self) -> None:
        document = self.minimum()
        document["manager"]["modpack"] = "not-yet-configured"
        endpoint = deployment_endpoint(self.write(document), "manager")
        self.assertEqual(endpoint.host, "manager.example")
        self.assertEqual(endpoint.user, "minecraft")
        self.assertEqual(endpoint.directory, "/srv/minecraft")

    def test_runtime_endpoints_reject_root_user(self) -> None:
        manager = self.minimum()
        manager["manager"]["user"] = "root"
        with self.assertRaisesRegex(ConfigError, "不能是 root"):
            load_manager(self.write(manager))

        monitor = self.minimum()
        monitor["monitor"] = {
            "enabled": True,
            "host": "monitor.example",
            "user": "root",
            "dir": "/srv/monitor",
            "timezone": "Asia/Shanghai",
            "manager": {
                "power-api": {
                    "provider": "tencent",
                    "instance-id": "manager",
                    "instance-region": "ap-test",
                },
            },
        }
        with self.assertRaisesRegex(ConfigError, "不能是 root"):
            load_monitor(self.write(monitor))

        monitor["monitor"]["enabled"] = False
        with self.assertRaisesRegex(ConfigError, "不能是 root"):
            load_monitor(self.write(monitor))

    def test_monitor_requires_cloud_identity(self) -> None:
        document = self.minimum()
        document["monitor"] = {
            "enabled": True,
            "host": "monitor.example",
            "user": "monitor",
            "dir": "/srv/monitor",
            "timezone": "Asia/Shanghai",
            "manager": {},
        }
        with self.assertRaisesRegex(ConfigError, "monitor.manager.power-api"):
            load(self.write(document))

    def test_provider_rejects_unsafe_id(self) -> None:
        document = self.minimum()
        document["manager"]["auto-stop"] = True
        document["manager"]["stop-at"] = "23:05"
        document["manager"]["modpack"].update({"auto-stop": True, "stop-at": "23:00"})
        document["manager"]["power-api"] = {
            "provider": "../power", "instance-id": "manager",
            "instance-region": "local",
        }
        with self.assertRaisesRegex(ConfigError, "字母、数字和下划线"):
            load(self.write(document))

    def test_enabled_schedule_requires_time(self) -> None:
        document = self.minimum()
        document["manager"]["modpack"]["auto-stop"] = True
        with self.assertRaisesRegex(ConfigError, "manager.modpack.stop-at"):
            load(self.write(document))

    def test_yaml_sexagesimal_time_is_normalized(self) -> None:
        document = self.minimum()
        document["manager"]["modpack"].update({
            "auto-stop": True,
            "stop-at": 23 * 60 + 5,
        })
        config = load(self.write(document))
        self.assertEqual(config.manager.modpack.stop_at, "23:05")

    def test_rejects_relative_remote_directory(self) -> None:
        document = self.minimum()
        document["manager"]["dir"] = "srv/minecraft"
        with self.assertRaisesRegex(ConfigError, "绝对路径"):
            load(self.write(document))

    def test_rejects_system_top_level_remote_directory(self) -> None:
        document = self.minimum()
        document["manager"]["dir"] = "/srv"
        with self.assertRaisesRegex(ConfigError, "系统顶层"):
            load(self.write(document))

    def test_rejects_remote_directory_with_shell_characters(self) -> None:
        document = self.minimum()
        document["manager"]["dir"] = "/srv/minecraft;touch-bad"
        with self.assertRaisesRegex(ConfigError, "安全的绝对路径"):
            load(self.write(document))

    def test_runtime_examples_are_direct_root_sections(self) -> None:
        source = ROOT / "terminal/config.yml.example"
        manager_text = (ROOT / "manager/config.yml.example").read_text(encoding="utf-8")
        monitor_text = (ROOT / "monitor/config.yml.example").read_text(encoding="utf-8")
        manager = yaml.safe_load(manager_text)
        monitor = yaml.safe_load(monitor_text)
        self.assertEqual(manager, runtime_section(source, "manager"))
        self.assertEqual(monitor, runtime_section(source, "monitor"))
        self.assertEqual(manager_text, render_runtime_section(source, "manager"))
        self.assertEqual(monitor_text, render_runtime_section(source, "monitor"))

if __name__ == "__main__":
    unittest.main()
