from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from lib.config import ConfigError, load


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
        }
        with self.assertRaisesRegex(ConfigError, "manager.region"):
            load(self.write(document))

    def test_enabled_schedule_requires_time(self) -> None:
        document = self.minimum()
        document["manager"]["modpack"]["auto-stop"] = True
        with self.assertRaisesRegex(ConfigError, "manager.modpack.stop-at"):
            load(self.write(document))

    def test_yaml_sexagesimal_time_is_normalized(self) -> None:
        document = self.minimum()
        document["manager"].update({
            "auto-stop": True,
            "stop-at": 23 * 60 + 5,
            "region": "ap-shanghai",
            "instance-id": "ins-test",
        })
        config = load(self.write(document))
        self.assertEqual(config.manager.stop_at, "23:05")

    def test_rejects_relative_remote_directory(self) -> None:
        document = self.minimum()
        document["manager"]["dir"] = "srv/minecraft"
        with self.assertRaisesRegex(ConfigError, "绝对路径"):
            load(self.write(document))


if __name__ == "__main__":
    unittest.main()
