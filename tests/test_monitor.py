from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from monitor.src.config import ConfigError, load
from monitor.src.monitor.power import InstanceStatus, PowerError
from monitor.src.monitor.schedule import PowerSchedule, StateStore, latest_event
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
    def write(self, manager: dict, qqbot: dict | None = None) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "config.yml"
        document = {"manager": manager}
        if qqbot is not None:
            document["qqbot"] = qqbot
        path.write_text(yaml.safe_dump(document), encoding="utf-8")
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

    def test_qq_targets_and_admins_are_loaded(self) -> None:
        config = load(self.write({
            "power-api": {
                "provider": "tencent", "instance-id": "ins-test",
                "instance-region": "ap-test",
            },
            "auto-start": False,
            "auto-stop": False,
        }, {
            "targets": [{"type": "group", "id": "group-a"}],
            "admins": ["user-a"],
        }))
        self.assertEqual(config.qqbot.targets[0].kind, "group")
        self.assertEqual(config.qqbot.targets[0].identifier, "group-a")
        self.assertEqual(config.qqbot.admins, ("user-a",))

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
        schedule = PowerSchedule(True, "19:55", True, "23:05")
        self.assertEqual(
            latest_event(datetime(2026, 10, 16, 20, 0, tzinfo=ZONE), schedule).key,
            "2026-10-16:start",
        )
        self.assertEqual(
            latest_event(datetime(2026, 10, 16, 23, 30, tzinfo=ZONE), schedule).key,
            "2026-10-16:stop",
        )

    def test_latest_event_supports_window_across_midnight(self) -> None:
        schedule = PowerSchedule(True, "23:00", True, "02:00")
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
        self.schedule = PowerSchedule(True, "19:55", True, "23:05")

    async def test_due_event_runs_once_and_survives_restart(self) -> None:
        now = datetime(2026, 10, 16, 20, 0, tzinfo=ZONE)
        power = FakePower("STOPPED")
        service = MonitorService(self.schedule, power, self.state)
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
        service = MonitorService(self.schedule, power, self.state)
        with self.assertRaises(PowerError):
            await service.run_once(now)
        self.assertFalse(self.state.contains("2026-10-16:stop"))
        power.failure = None
        report = await service.run_once(now)
        self.assertTrue(report.executed)
        self.assertTrue(self.state.contains("2026-10-16:stop"))

    async def test_problem_is_deduplicated_and_recovery_is_reported(self) -> None:
        alerts: list[str | None] = []

        async def alert(problem: str | None) -> None:
            alerts.append(problem)

        service = MonitorService(
            self.schedule, FakePower("RUNNING"), self.state, alert=alert,
        )
        await service._set_problem("API 暂时不可用")
        await service._set_problem("API 暂时不可用")
        await service._set_problem(None)
        await service._set_problem(None)
        self.assertEqual(alerts, ["API 暂时不可用", None])

    def test_state_mismatch_is_reported_after_processed_event(self) -> None:
        event = latest_event(datetime(2026, 10, 16, 20, 0, tzinfo=ZONE), self.schedule)
        report = type("Report", (), {
            "event": event,
            "executed": False,
            "status": InstanceStatus("STOPPED"),
        })()
        self.assertIn("定时开机", MonitorService._problem(report))


if __name__ == "__main__":
    unittest.main()
