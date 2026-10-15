from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from manager.lib.config import ManagerLimits
from manager.lib.power import PowerError, TencentPower


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

    def test_running_instance_uses_billing_safe_soft_stop(self) -> None:
        runner = FakeRunner()
        with patch.dict("os.environ", {"POWER_SECRET_ID": "id", "POWER_SECRET_KEY": "key"}):
            TencentPower(self.settings, self.root, runner, executable="tccli").stop()
        self.assertEqual(runner.actions, ["DescribeInstances", "StopInstances"])

    def test_stopped_instance_is_idempotent(self) -> None:
        runner = FakeRunner("STOPPED")
        with patch.dict("os.environ", {"POWER_SECRET_ID": "id", "POWER_SECRET_KEY": "key"}):
            TencentPower(self.settings, self.root, runner, executable="tccli").stop()
        self.assertEqual(runner.actions, ["DescribeInstances"])

    def test_credentials_are_required(self) -> None:
        with patch.dict("os.environ", {}, clear=True), self.assertRaisesRegex(PowerError, "POWER_SECRET"):
            TencentPower(self.settings, self.root, FakeRunner(), executable="tccli")


if __name__ == "__main__":
    unittest.main()
