from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from manager.lib.config import ManagerLimits
from manager.lib.power import Power, PowerError, PowerTarget, webhook_adapter
from manager.lib.providers import create_power
from manager.lib.tencent import tencent_adapter


class FakeRunner:
    def __init__(self, state: str = "RUNNING") -> None:
        self.state = state
        self.actions: list[str] = []

    def __call__(self, arguments: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        action = arguments[2]
        self.actions.append(action)
        if action == "DescribeInstances":
            response = {"Response": {"InstanceSet": [{
                "InstanceId": "ins-test", "InstanceState": self.state,
            }]}}
        else:
            self.assert_stop_arguments(arguments)
            response = {"Response": {"RequestId": "request"}}
        return subprocess.CompletedProcess(arguments, 0, json.dumps(response), "")

    def assert_stop_arguments(self, arguments: list[str]) -> None:
        if "STOP_CHARGING" not in arguments or "SOFT" not in arguments:
            raise AssertionError(arguments)


class PowerTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings = ManagerLimits(
            1, "4g", 4 * 1024**3, 30, 30, True, "23:00", 60, 2,
            True, 5, "ap-test", "ins-test",
        )
        self.target = PowerTarget("ins-test", "ap-test")

    def test_running_instance_uses_billing_safe_soft_stop(self) -> None:
        runner = FakeRunner()
        Power(tencent_adapter(
            self.target, "id", "key", manager_root=self.root,
            runner=runner, executable="tccli",
        ), wait_after_action=False).stop()
        self.assertEqual(runner.actions, ["DescribeInstances", "StopInstances"])

    def test_stopped_instance_is_idempotent(self) -> None:
        runner = FakeRunner("STOPPED")
        Power(tencent_adapter(
            self.target, "id", "key", manager_root=self.root,
            runner=runner, executable="tccli",
        ), wait_after_action=False).stop()
        self.assertEqual(runner.actions, ["DescribeInstances"])

    def test_credentials_are_required(self) -> None:
        with patch.dict("os.environ", {}, clear=True), self.assertRaisesRegex(PowerError, "POWER_SECRET"):
            tencent_adapter(
                self.target, "", "", manager_root=self.root,
                runner=FakeRunner(), executable="tccli",
            )

    def test_webhook_reads_fixed_token_and_uses_normalized_payload(self) -> None:
        calls: list[tuple[str, dict]] = []

        def api(action, payload):
            calls.append((action, payload))
            return {"state": "RUNNING"} if action == "status" else {}

        Power(webhook_adapter(
            PowerTarget("manager", "local"), "https://power.example/api", "token",
            api_call=api,
        ), wait_after_action=False).stop()
        self.assertEqual([action for action, _ in calls], ["status", "stop"])
        self.assertEqual(calls[0][1], {
            "action": "status", "instance-id": "manager", "instance-region": "local",
        })

    def test_factory_selects_webhook_provider(self) -> None:
        settings = ManagerLimits(
            1, "4g", 4 * 1024**3, 30, 30,
            poweroff_enabled=True, power_region="local", power_instance_id="manager",
            power_provider="webhook", power_url="https://power.example/api",
        )
        with patch.dict("os.environ", {"POWER_SECRET_KEY": "token"}):
            self.assertIsInstance(create_power(settings, self.root), Power)


if __name__ == "__main__":
    unittest.main()
