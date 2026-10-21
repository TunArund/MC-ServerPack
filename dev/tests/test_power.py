from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from manager.src.config import ManagerLimits
from manager.src.power_sdk import Power, PowerError, PowerTarget
from manager.src.power_sdk import ProviderError, list_providers, load_provider
from manager.src.providers import tencent
from manager.src.schedule import create_power


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

    def tencent(self, runner: FakeRunner, secret_id: str = "id", secret_key: str = "key") -> Power:
        def api(action: str, payload: dict) -> dict:
            runner.actions.append(action)
            if action == "DescribeInstances":
                return {"InstanceSet": [{
                    "InstanceId": "ins-test", "InstanceState": runner.state,
                }]}
            if action == "StopInstances":
                self.assertEqual(payload["StopType"], "SOFT")
                self.assertEqual(payload["StoppedMode"], "STOP_CHARGING")
            return {}
        patcher = patch.object(tencent, "_create_api", return_value=api)
        patcher.start()
        self.addCleanup(patcher.stop)
        return Power(tencent, self.target, secret_id, secret_key, wait_after_action=False)

    def test_running_instance_uses_billing_safe_soft_stop(self) -> None:
        runner = FakeRunner()
        self.tencent(runner).stop()
        self.assertEqual(runner.actions, ["DescribeInstances", "StopInstances"])

    def test_stopped_instance_is_idempotent(self) -> None:
        runner = FakeRunner("STOPPED")
        self.tencent(runner).stop()
        self.assertEqual(runner.actions, ["DescribeInstances"])

    def test_credentials_are_required(self) -> None:
        with patch.dict("os.environ", {}, clear=True), self.assertRaisesRegex(PowerError, "POWER_SECRET"):
            Power(tencent, self.target, "", "").status()

    def test_webhook_reads_fixed_token_and_uses_normalized_payload(self) -> None:
        calls: list[tuple[str, str, str, str, str]] = []

        def function(action: str):
            def call(instance: str, region: str, secret_id: str, secret_key: str):
                calls.append((action, instance, region, secret_id, secret_key))
                return {"state": "RUNNING"} if action == "status" else {}
            return call

        adapter = SimpleNamespace(**{action: function(action) for action in ("status", "start", "stop")})
        Power(
            adapter, PowerTarget("manager", "local"), "", "token",
            wait_after_action=False,
        ).stop()
        self.assertEqual([item[0] for item in calls], ["status", "stop"])
        self.assertEqual(calls[0], ("status", "manager", "local", "", "token"))

    def test_factory_selects_custom_adapter(self) -> None:
        settings = ManagerLimits(
            1, "4g", 4 * 1024**3, 30, 30,
            poweroff_enabled=True, power_region="local", power_instance_id="manager",
            power_provider="xxxaaa",
        )
        with (
            patch.dict("os.environ", {"POWER_SECRET_KEY": "token"}),
            patch("manager.src.power_sdk.load_provider", return_value=SimpleNamespace()),
        ):
            self.assertIsInstance(create_power(settings, self.root), Power)

    def test_provider_is_discovered_and_interface_is_checked(self) -> None:
        providers = self.root / "providers"
        providers.mkdir()
        (providers / "xxxaaa.py").write_text(
            "def status(instance_id, region, secret_id, secret_key): return {'state': 'RUNNING'}\n"
            "def start(instance_id, region, secret_id, secret_key): return {}\n"
            "def stop(instance_id, region, secret_id, secret_key): return {}\n",
            encoding="utf-8",
        )
        adapter = load_provider("xxxaaa", providers_dir=providers)
        self.assertEqual(adapter.status("i", "r", "", "token"), {"state": "RUNNING"})
        self.assertEqual(list_providers(providers_dir=providers), ("xxxaaa",))

    def test_provider_requires_all_actions(self) -> None:
        providers = self.root / "providers"
        providers.mkdir()
        (providers / "broken.py").write_text("VALUE = 1\n", encoding="utf-8")
        with self.assertRaisesRegex(ProviderError, "status"):
            load_provider("broken", providers_dir=providers)

    def test_provider_id_rejects_hyphen_and_paths(self) -> None:
        for provider in ("bad-name", "../bad", "bad.py"):
            with self.subTest(provider=provider), self.assertRaisesRegex(
                ProviderError, "字母、数字和下划线",
            ):
                load_provider(provider, providers_dir=self.root)


if __name__ == "__main__":
    unittest.main()
