from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from manager.lib.config import ConfigError, load_limits


class ManagerConfigTests(unittest.TestCase):
    def write(self, document: dict) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "config.yml"
        path.write_text(yaml.safe_dump(document), encoding="utf-8")
        return path

    def minimum(self) -> dict:
        return {
            "auto-stop": False,
            "stop-at": "23:05",
            "modpack": {
                "auto-stop": False,
                "stop-at": "23:00",
                "max-running": 1,
                "mem-limit": "4g",
                "start-timeout": 300,
                "stop-timeout": 180,
            },
        }

    def test_schedule_defaults_to_disabled(self) -> None:
        settings = load_limits(self.write(self.minimum()))
        self.assertFalse(settings.schedule_enabled)
        self.assertFalse(settings.poweroff_enabled)
        self.assertEqual(settings.stop_at, "23:00")

    def test_schedule_and_poweroff_are_parsed(self) -> None:
        document = self.minimum()
        document.update({
            "auto-stop": True,
            "stop-at": "23:05",
            "region": "ap-shanghai",
            "instance-id": "ins-test",
        })
        document["modpack"].update({
            "auto-stop": True,
            "stop-notify-elast": 30,
            "stop-notify-step": 3,
        })
        settings = load_limits(self.write(document))
        self.assertTrue(settings.schedule_enabled)
        self.assertEqual(settings.stop_at, "23:00")
        self.assertEqual(settings.notify_elast, 30)
        self.assertEqual(settings.notify_step, 3)
        self.assertTrue(settings.poweroff_enabled)
        self.assertEqual(settings.poweroff_at, "23:05")
        self.assertEqual(settings.power_region, "ap-shanghai")
        self.assertEqual(settings.power_instance_id, "ins-test")

    def test_poweroff_requires_auto_stop(self) -> None:
        document = self.minimum()
        document.update({
            "auto-stop": True,
            "region": "ap-shanghai",
            "instance-id": "ins-test",
        })
        with self.assertRaisesRegex(ConfigError, "需要 modpack.auto-stop=true"):
            load_limits(self.write(document))

    def test_schedule_rejects_invalid_clock(self) -> None:
        document = self.minimum()
        document["modpack"]["stop-at"] = "24:00"
        with self.assertRaisesRegex(ConfigError, "HH:MM"):
            load_limits(self.write(document))


if __name__ == "__main__":
    unittest.main()
