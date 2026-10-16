from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from monitor.src.config import ConfigError, MonitorConfig, PowerApiConfig, ScheduleConfig, load
from monitor.src.monitor.power import InstanceStatus, PowerError
from monitor.src.monitor.schedule import StateStore, latest_event
from monitor.src.monitor.service import MonitorService


ZONE = ZoneInfo("Asia/Shanghai")


class FakePower:
    def __init__(self, state: str) -> None:
        self.state = state
        self.calls: list[str] = []
        self.failure: Exception | None = None

    def status(self) -> InstanceStatus:
        self.calls.append("status")
        return InstanceStatus(self.state)

    def start(self) -> InstanceStatus:
        self.calls.append("start")
        if self.failure:
            raise self.failure
        self.state = "STARTING"
        return InstanceStatus(self.state)

    def stop(self, current=None) -> InstanceStatus:
        self.calls.append("stop")
        if self.failure:
            raise self.failure
        self.state = "STOPPING"
        return InstanceStatus(self.state)


class MonitorConfigTests(unittest.TestCase):
    def write(self, manager: dict) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "config.yml"
        path.write_text(yaml.safe_dump({"manager": manager}), encoding="utf-8")
        return path

    def test_original_monitor_config_shape_is_loaded(self) -> None:
        config = load(self.write({
            "power-api": {
                "provider": "tencent", "instance-id": "ins-test",
                "instance-region": "ap-test",
            },
            "auto-start": True, "start-at": "19:55",
            "auto-stop": True, "stop-at": "23:05",
        }))
        self.assertEqual(config.power_api.instance_id, "ins-test")
        self.assertEqual(config.schedule.start_at, "19:55")
        self.assertEqual(config.schedule.stop_at, "23:05")

    def test_equal_enabled_event_times_are_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigError, "不能相同"):
            load(self.write({
                "power-api": {
                    "provider": "tencent", "instance-id": "ins-test",
                    "instance-region": "ap-test",
                },
                "auto-start": True, "start-at": "20:00",
                "auto-stop": True, "stop-at": "20:00",
            }))


class MonitorScheduleTests(unittest.TestCase):
    def test_latest_event_tracks_normal_daily_window(self) -> None:
        schedule = ScheduleConfig(True, "19:55", True, "23:05")
        self.assertEqual(
            latest_event(datetime(2026, 10, 16, 20, 0, tzinfo=ZONE), schedule).key,
            "2026-10-16:start",
        )
        self.assertEqual(
            latest_event(datetime(2026, 10, 16, 23, 30, tzinfo=ZONE), schedule).key,
            "2026-10-16:stop",
        )

    def test_latest_event_supports_window_across_midnight(self) -> None:
        schedule = ScheduleConfig(True, "23:00", True, "02:00")
        self.assertEqual(
            latest_event(datetime(2026, 10, 17, 1, 0, tzinfo=ZONE), schedule).key,
            "2026-10-16:start",
        )
        self.assertEqual(
            latest_event(datetime(2026, 10, 17, 3, 0, tzinfo=ZONE), schedule).key,
            "2026-10-17:stop",
        )


class MonitorServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = StateStore(Path(temporary.name) / "state.json")
        self.config = MonitorConfig(
            PowerApiConfig("tencent", "ins-test", "ap-test"),
            ScheduleConfig(True, "19:55", True, "23:05"),
        )

    async def test_due_event_runs_once_and_survives_restart(self) -> None:
        now = datetime(2026, 10, 16, 20, 0, tzinfo=ZONE)
        power = FakePower("STOPPED")
        service = MonitorService(self.config, power, self.state)
        first = await service.run_once(now)
        second = await service.run_once(now)
        self.assertTrue(first.executed)
        self.assertFalse(second.executed)
        self.assertEqual(power.calls, ["status", "start", "status"])
        reloaded = StateStore(self.state.path)
        self.assertTrue(reloaded.contains("2026-10-16:start"))

    async def test_failed_action_is_retried(self) -> None:
        now = datetime(2026, 10, 16, 23, 10, tzinfo=ZONE)
        power = FakePower("RUNNING")
        power.failure = PowerError("temporary")
        service = MonitorService(self.config, power, self.state)
        with self.assertRaises(PowerError):
            await service.run_once(now)
        self.assertFalse(self.state.contains("2026-10-16:stop"))
        power.failure = None
        report = await service.run_once(now)
        self.assertTrue(report.executed)
        self.assertTrue(self.state.contains("2026-10-16:stop"))


if __name__ == "__main__":
    unittest.main()
