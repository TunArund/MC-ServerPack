from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import yaml

from monitor.src.app import AsyncPower
from monitor.src.config import ConfigError, load, qq_credentials
from monitor.src.power_sdk import InstanceStatus, PowerError
from monitor.src.schedule import (
    MonitorService, PowerSchedule, StateStore, latest_event, next_start_event,
)


ZONE = ZoneInfo("Asia/Shanghai")


async def immediate_to_thread(function, *arguments, **options):
    return function(*arguments, **options)


class FakeStopPacks:
    def __init__(self) -> None:
        self.calls = 0
        self.failure: Exception | None = None

    def __call__(self) -> None:
        self.calls += 1
        if self.failure:
            raise self.failure


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
        path.write_text(yaml.safe_dump({
            "timezone": "Asia/Shanghai", "manager": manager,
        }), encoding="utf-8")
        return path

    def test_monitor_config_shape_is_loaded(self) -> None:
        config = load(self.write({
            "power-api": {
                "provider": "tencent", "instance-id": "ins-test",
                "instance-region": "ap-test",
            },
            "api": {"host": "mc.example.com", "port": 27016},
            "auto-start": True, "start-at": "19:55",
            "auto-stop": True, "stop-at": "23:05",
        }))
        self.assertEqual(config.power_api.instance_id, "ins-test")
        self.assertEqual(config.timezone, "Asia/Shanghai")
        self.assertEqual(config.schedule.start_at, "19:55")
        self.assertEqual(config.schedule.stop_at, "23:05")

    def test_manager_api_uses_host_field(self) -> None:
        config = load(self.write({
            "power-api": {
                "provider": "tencent", "instance-id": "ins-test",
                "instance-region": "ap-test",
            },
            "api": {"host": "mc.example.com", "port": 27016},
        }))
        self.assertEqual(config.manager_api.host, "mc.example.com")
        self.assertEqual(config.manager_api.port, 27016)

    def test_manager_api_requires_host(self) -> None:
        with self.assertRaisesRegex(ConfigError, "manager.api.host"):
            load(self.write({
                "power-api": {
                    "provider": "tencent", "instance-id": "ins-test",
                    "instance-region": "ap-test",
                },
                "api": {},
            }))

    def test_rejects_invalid_timezone(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "config.yml"
        path.write_text(yaml.safe_dump({
            "timezone": "Mars/Olympus",
            "manager": {},
        }), encoding="utf-8")
        with self.assertRaisesRegex(ConfigError, "IANA 时区"):
            load(path)

    def test_equal_enabled_event_times_are_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigError, "不能相同"):
            load(self.write({
                "power-api": {
                    "provider": "tencent", "instance-id": "ins-test",
                    "instance-region": "ap-test",
                },
                "api": {"host": "mc.example.com"},
                "auto-start": True, "start-at": "20:00",
                "auto-stop": True, "stop-at": "20:00",
            }))

    def test_qq_credentials_use_existing_secret_names(self) -> None:
        with patch.dict("os.environ", {
            "QQBOT_APP_ID": "app-id",
            "QQBOT_APP_KEY": "app-key",
        }, clear=True):
            self.assertEqual(qq_credentials(), ("app-id", "app-key"))

    def test_partial_qq_credentials_are_rejected(self) -> None:
        with patch.dict("os.environ", {"QQBOT_APP_ID": "app-id"}, clear=True):
            with self.assertRaisesRegex(ConfigError, "必须同时配置"):
                qq_credentials()


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

    def test_next_start_does_not_treat_past_time_as_pending(self) -> None:
        schedule = PowerSchedule(True, "20:00", True, "23:05")
        event = next_start_event(datetime(2026, 10, 18, 21, 0, tzinfo=ZONE), schedule)
        self.assertEqual(event.key, "2026-10-19:start")


class MonitorServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        thread_patch = patch(
            "monitor.src.schedule.asyncio.to_thread", new=immediate_to_thread,
        )
        thread_patch.start()
        self.addCleanup(thread_patch.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = StateStore(Path(temporary.name) / "state.json")
        self.schedule = PowerSchedule(True, "19:55", True, "23:05")
        self.state.initialize(None, datetime(2026, 10, 16, tzinfo=ZONE).date())

    async def test_first_start_records_current_event_without_executing_it(self) -> None:
        self.state.path.unlink()
        self.state = StateStore(self.state.path)
        power = FakePower("STOPPED")
        service = MonitorService(self.schedule, power, self.state)
        report = await service.run_once(datetime(2026, 10, 16, 20, 0, tzinfo=ZONE))
        self.assertFalse(report.executed)
        self.assertEqual(power.calls, ["status"])
        self.assertTrue(self.state.initialized)
        self.assertTrue(self.state.contains("2026-10-16:start"))

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
        stop_packs = FakeStopPacks()
        service = MonitorService(
            self.schedule, power, self.state, stop_packs=stop_packs,
        )
        with self.assertRaises(PowerError):
            await service.run_once(now)
        self.assertFalse(self.state.contains("2026-10-16:stop"))
        power.failure = None
        report = await service.run_once(now)
        self.assertTrue(report.executed)
        self.assertTrue(self.state.contains("2026-10-16:stop"))
        self.assertEqual(stop_packs.calls, 2)

    async def test_stop_event_stops_packs_before_power(self) -> None:
        order: list[str] = []

        def stop_packs() -> None:
            order.append("packs")

        class OrderedPower(FakePower):
            def stop(self, current=None) -> InstanceStatus:
                order.append("power")
                return super().stop(current)

        service = MonitorService(
            self.schedule, OrderedPower("RUNNING"), self.state,
            stop_packs=stop_packs,
        )
        report = await service.run_once(
            datetime(2026, 10, 16, 23, 10, tzinfo=ZONE),
        )
        self.assertTrue(report.executed)
        self.assertEqual(order, ["packs", "power"])

    async def test_stop_all_failure_prevents_power_off(self) -> None:
        stop_packs = FakeStopPacks()
        stop_packs.failure = RuntimeError("Manager 不可用")
        power = FakePower("RUNNING")
        service = MonitorService(
            self.schedule, power, self.state, stop_packs=stop_packs,
        )
        with self.assertRaisesRegex(RuntimeError, "Manager 不可用"):
            await service.run_once(
                datetime(2026, 10, 16, 23, 10, tzinfo=ZONE),
            )
        self.assertEqual(power.calls, ["status"])
        self.assertFalse(self.state.contains("2026-10-16:stop"))

    async def test_stop_without_manager_api_is_refused(self) -> None:
        power = FakePower("RUNNING")
        service = MonitorService(self.schedule, power, self.state)
        with self.assertRaisesRegex(PowerError, "未配置 Manager API"):
            await service.run_once(
                datetime(2026, 10, 16, 23, 10, tzinfo=ZONE),
            )
        self.assertEqual(power.calls, ["status"])

    async def test_problem_is_deduplicated_and_recovery_is_reported(self) -> None:
        alerts: list[tuple[str, str]] = []

        async def alert(kind: str, content: str) -> None:
            alerts.append((kind, content))

        service = MonitorService(
            self.schedule, FakePower("RUNNING"), self.state, alert=alert,
        )
        problem = ("check-failed", "API 暂时不可用")
        await service._set_problem(problem)
        await service._set_problem(problem)
        await service._set_problem(None)
        await service._set_problem(None)
        self.assertEqual(alerts, [problem, ("recovered", problem[1])])

    async def test_manual_stop_explains_but_does_not_cancel_start_expectation(self) -> None:
        now = datetime(2026, 10, 18, 20, 44, tzinfo=ZONE)
        service = MonitorService(self.schedule, FakePower("STOPPED"), self.state)
        event = await service.record_manual_power("stop", now)
        self.assertEqual(event.key, "2026-10-18:start")
        self.state.mark(event.key, now.date())
        report = await service.run_once(now)
        problem = service._problem(report)
        self.assertEqual(problem[0], "state-mismatch-start")
        self.assertIn("人工提交了关机", problem[1])
        self.assertFalse(StateStore(self.state.path).is_cancelled(event.key))

    async def test_manual_power_record_survives_restart(self) -> None:
        now = datetime(2026, 10, 18, 20, 44, tzinfo=ZONE)
        service = MonitorService(self.schedule, FakePower("RUNNING"), self.state)
        await service.record_manual_power("stop", now)
        reloaded = StateStore(self.state.path)
        self.assertEqual(reloaded.manual_action, "stop")
        self.assertEqual(reloaded.manual_at, now)

    async def test_recovery_is_suppressed_during_alert_snooze(self) -> None:
        alerts: list[tuple[str, str]] = []

        async def alert(kind: str, content: str) -> None:
            alerts.append((kind, content))

        now = datetime(2026, 10, 18, 20, 0, tzinfo=ZONE)
        service = MonitorService(
            self.schedule, FakePower("RUNNING"), self.state, alert=alert,
        )
        problem = ("check-failed", "API 暂时不可用")
        await service._set_problem(problem, now)
        await service.snooze_alerts(1, now)
        await service._set_problem(None, now)
        self.assertEqual(alerts, [problem])

    async def test_cancel_next_start_and_snooze_survive_restart(self) -> None:
        now = datetime(2026, 10, 18, 16, 0, tzinfo=ZONE)
        service = MonitorService(self.schedule, FakePower("STOPPED"), self.state)
        event = await service.cancel_next_start(now)
        until = await service.snooze_alerts(2, now)
        self.assertEqual(event.key, "2026-10-18:start")
        self.assertEqual(until.hour, 18)
        reloaded = StateStore(self.state.path)
        self.assertTrue(reloaded.contains(event.key))
        self.assertTrue(reloaded.alerts_snoozed(datetime(2026, 10, 18, 17, 0, tzinfo=ZONE)))

    async def test_cancelled_start_can_be_restored(self) -> None:
        now = datetime(2026, 10, 18, 16, 0, tzinfo=ZONE)
        service = MonitorService(self.schedule, FakePower("STOPPED"), self.state)
        cancelled = await service.cancel_next_start(now)
        restored = await service.restore_next_start(now)
        self.assertEqual(restored, cancelled)
        reloaded = StateStore(self.state.path)
        self.assertFalse(reloaded.contains(restored.key))
        with self.assertRaisesRegex(Exception, "没有被取消"):
            await service.restore_next_start(now)

    async def test_stable_power_transition_is_persisted(self) -> None:
        power = FakePower("STOPPED")
        service = MonitorService(self.schedule, power, self.state)
        first = await service.run_once(datetime(2026, 10, 18, 16, 0, tzinfo=ZONE))
        self.assertIsNone(first.transition)
        power.state = "RUNNING"
        second = await service.run_once(datetime(2026, 10, 18, 16, 1, tzinfo=ZONE))
        self.assertEqual(second.transition, "RUNNING")
        reloaded = StateStore(self.state.path)
        self.assertEqual(reloaded.power_state, "RUNNING")
        self.assertEqual(reloaded.notified_power_state, "STOPPED")
        third = await service.run_once(datetime(2026, 10, 18, 16, 2, tzinfo=ZONE))
        self.assertEqual(third.transition, "RUNNING")
        self.state.ack_power_notification("RUNNING", datetime(2026, 10, 18).date())
        fourth = await service.run_once(datetime(2026, 10, 18, 16, 3, tzinfo=ZONE))
        self.assertIsNone(fourth.transition)

    async def test_cancelled_start_does_not_report_state_mismatch(self) -> None:
        before = datetime(2026, 10, 18, 16, 0, tzinfo=ZONE)
        service = MonitorService(self.schedule, FakePower("STOPPED"), self.state)
        await service.cancel_next_start(before)
        report = await service.run_once(
            datetime(2026, 10, 18, 20, 0, tzinfo=ZONE),
        )
        self.assertIsNone(report.event)
        self.assertIsNone(service._problem(report))

    async def test_snoozed_problem_is_sent_after_snooze_expires(self) -> None:
        alerts: list[tuple[str, str]] = []

        async def alert(kind: str, content: str) -> None:
            alerts.append((kind, content))

        now = datetime(2026, 10, 18, 16, 0, tzinfo=ZONE)
        service = MonitorService(
            self.schedule, FakePower("RUNNING"), self.state, alert=alert,
        )
        await service.snooze_alerts(1, now)
        problem = ("check-failed", "API 暂时不可用")
        await service._set_problem(problem, now)
        self.assertEqual(alerts, [])
        await service._set_problem(problem, datetime(2026, 10, 18, 17, 1, tzinfo=ZONE))
        self.assertEqual(alerts, [problem])

    def test_state_mismatch_is_reported_after_processed_event(self) -> None:
        event = latest_event(datetime(2026, 10, 16, 20, 0, tzinfo=ZONE), self.schedule)
        report = type("Report", (), {
            "event": event,
            "executed": False,
            "status": InstanceStatus("STOPPED"),
        })()
        kind, content = MonitorService(
            self.schedule, FakePower("STOPPED"), self.state,
        )._problem(report)
        self.assertEqual(kind, "state-mismatch-start")
        self.assertIn("计划期望实例已开机", content)


class AsyncPowerTests(unittest.IsolatedAsyncioTestCase):
    async def test_manual_stop_stops_packs_before_power_and_records(self) -> None:
        order: list[str] = []

        class Power:
            def stop(self) -> InstanceStatus:
                order.append("power")
                return InstanceStatus("STOPPING")

        async def before_stop() -> None:
            order.append("packs")

        async def record(action: str) -> None:
            order.append(action)

        power = AsyncPower(Power(), record, before_stop=before_stop)
        with patch(
            "monitor.src.app.asyncio.to_thread", new=immediate_to_thread,
        ):
            await power.stop()
        self.assertEqual(order, ["packs", "power", "stop"])

    async def test_manual_stop_is_aborted_when_pack_stop_fails(self) -> None:
        order: list[str] = []

        class Power:
            def stop(self) -> InstanceStatus:
                order.append("power")
                return InstanceStatus("STOPPING")

        async def before_stop() -> None:
            raise RuntimeError("Manager 不可用")

        async def record(action: str) -> None:
            order.append(action)

        power = AsyncPower(Power(), record, before_stop=before_stop)
        with self.assertRaisesRegex(RuntimeError, "Manager 不可用"):
            await power.stop()
        self.assertEqual(order, [])


if __name__ == "__main__":
    unittest.main()
