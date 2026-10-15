from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from lib.config import ConfigError, load, render_runtime_section, runtime_section


ROOT = Path(__file__).resolve().parents[1]


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
                "auto-stop": False,
                "modpack": {"java": [17, 21], "mem-limit": "16g"},
            },
            "monitor": {"enabled": False},
            "cos": {"enabled": False},
        }

    def test_minimum_disabled_optional_services(self) -> None:
        config = load(self.write(self.minimum()))
        self.assertEqual(config.manager.endpoint.host, "manager.example")
        self.assertEqual(config.manager.modpack.java, (17, 21))
        self.assertFalse(config.monitor.enabled)
        self.assertIsNone(config.monitor.endpoint)

    def test_monitor_requires_cloud_identity(self) -> None:
        document = self.minimum()
        document["monitor"] = {
            "enabled": True,
            "host": "monitor.example",
            "user": "monitor",
            "dir": "/srv/monitor",
            "manager": {},
        }
        with self.assertRaisesRegex(ConfigError, "monitor.manager.region"):
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
        source = ROOT / "config.yml.example"
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
