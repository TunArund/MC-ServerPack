from __future__ import annotations

import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from lib.cli import CliServices, dispatch
from lib.config import load as load_config
from lib.deploy import DeploymentService
from lib.modpacks import ModpackError, ModpackService, validate_pack_id
from lib.power import InstanceStatus, Power, PowerError, PowerTarget
from lib.providers import tencent_api
from lib.remote import CommandError, archive_tree, cleanup_archive, shell_join


class FakeCloud:
    def __init__(self) -> None:
        self.calls: list[object] = []

    def status(self):
        self.calls.append("status")
        return InstanceStatus("RUNNING", "203.0.113.1")

    def start(self):
        self.calls.append("start")
        return InstanceStatus("RUNNING")

    def stop(self, current=None):
        self.calls.append(("stop", current.state if current else None))
        return InstanceStatus("STOPPED")


class FakeService:
    def __init__(self) -> None:
        self.calls: list[object] = []
        self.cloud = FakeCloud()
        self.stop_error: Exception | None = None

    def check(self, target: str) -> None:
        self.calls.append(("check", target))

    def deploy_manager(self) -> None:
        self.calls.append("deploy-manager")

    def deploy_monitor(self) -> None:
        self.calls.append("deploy-monitor")

    def deploy_pack(self, pack_id: str, *, force: bool) -> None:
        self.calls.append(("deploy-pack", pack_id, force))

    def create_pack(self, pack_id: str) -> Path:
        self.calls.append(("create-pack", pack_id))
        return Path("manager/modpacks") / pack_id

    def remote_manager(self, arguments: list[str], *, tty: bool = False) -> None:
        self.calls.append(("remote", arguments, tty))

    def power(self):
        return self.cloud

    def stop_modpacks(self) -> None:
        self.calls.append("stop-modpacks")
        if self.stop_error:
            raise self.stop_error


class FakeTransport:
    def __init__(self, archive: Path) -> None:
        self.archive_path = archive
        self.ssh_calls: list[tuple[str, list[str], dict]] = []
        self.uploads: list[tuple[str, bytes]] = []
        self.cleaned = False
        self.excluded: set[str] = set()

    def ssh(self, target: str, arguments: list[str], **kwargs):
        self.ssh_calls.append((target, arguments, kwargs))
        return subprocess.CompletedProcess(arguments, 1 if arguments[:2] == ["test", "-e"] else 0)

    def scp(self, source: Path, destination: str) -> None:
        self.uploads.append((destination, source.read_bytes()))

    def archive(self, source: Path, exclude=None) -> Path:
        self.excluded = set(exclude or ())
        return self.archive_path

    def cleanup(self, archive: Path) -> None:
        self.cleaned = True


class TerminalCliTests(unittest.TestCase):
    def test_pack_arguments_are_forwarded_without_shell_joining(self) -> None:
        service = FakeService()
        self.assertEqual(dispatch(["pack", "console", "demo", "say", "hello world"], service), 0)
        self.assertEqual(service.calls, [("remote", ["console", "demo", "say", "hello world"], False)])

    def test_follow_logs_requests_tty(self) -> None:
        service = FakeService()
        dispatch(["pack", "logs", "demo", "--lines", "20", "--follow"], service)
        self.assertEqual(service.calls, [("remote", ["logs", "demo", "--lines", "20", "--follow"], True)])

    def test_deploy_all_keeps_runtime_deployments_separate(self) -> None:
        service = FakeService()
        dispatch(["deploy", "all"], service)
        self.assertEqual(service.calls, ["deploy-manager", "deploy-monitor"])

    def test_pack_create_is_local(self) -> None:
        service = FakeService()
        dispatch(["pack", "create", "demo"], service)
        self.assertEqual(service.calls, [("create-pack", "demo")])

    def test_force_poweroff_is_explicit(self) -> None:
        service = FakeService()
        service.stop_error = CommandError("SSH failed")
        dispatch(["manager", "stop", "--force"], service)
        self.assertEqual(service.calls, ["stop-modpacks"])
        self.assertEqual(service.cloud.calls, ["status", ("stop", "RUNNING")])

    def test_stop_failure_prevents_poweroff_by_default(self) -> None:
        service = FakeService()
        service.stop_error = CommandError("SSH failed")
        with patch("builtins.input", return_value="yes"), self.assertRaises(CommandError):
            dispatch(["manager", "stop"], service)
        self.assertEqual(service.calls, ["stop-modpacks"])
        self.assertEqual(service.cloud.calls, ["status"])

    def test_pack_id_is_checked_before_remote_command(self) -> None:
        service = FakeService()
        with self.assertRaises(ModpackError):
            dispatch(["pack", "status", "bad_name"], service)
        self.assertFalse(service.calls)


class PowerTests(unittest.TestCase):
    def target(self) -> PowerTarget:
        return PowerTarget("ins-test")

    def tencent(self, api, *, sleeper=lambda _: None) -> Power:
        patcher = patch.object(tencent_api, "_create_api", return_value=api)
        patcher.start()
        self.addCleanup(patcher.stop)
        return Power(tencent_api, self.target(), "id", "key", sleeper=sleeper)

    def test_start_is_idempotent_and_waits_for_running(self) -> None:
        states = iter(("STOPPED", "RUNNING"))
        actions: list[str] = []

        def api(action, payload):
            actions.append(action)
            if action == "DescribeInstances":
                return {"InstanceSet": [{"InstanceId": "ins-test", "InstanceState": next(states)}]}
            return {}

        result = self.tencent(api).start()
        self.assertEqual(result.state, "RUNNING")
        self.assertEqual(actions, ["DescribeInstances", "StartInstances", "DescribeInstances"])

    def test_stop_only_handles_soft_poweroff(self) -> None:
        descriptions = iter(("RUNNING", "STOPPED"))
        actions: list[str] = []

        def api(action, payload):
            actions.append(action)
            if action == "DescribeInstances":
                return {"InstanceSet": [{"InstanceId": "ins-test", "InstanceState": next(descriptions)}]}
            return {}

        power = self.tencent(api)
        current = power.status()
        result = power.stop(current)
        self.assertEqual(result.state, "STOPPED")
        self.assertEqual(actions, ["DescribeInstances", "StopInstances", "DescribeInstances"])

    def test_invalid_instance_state_does_not_poweroff(self) -> None:
        actions: list[str] = []

        def api(action, payload):
            actions.append(action)
            if action == "DescribeInstances":
                return {"InstanceSet": [{"InstanceId": "ins-test", "InstanceState": "REBOOTING"}]}
            return {}

        with self.assertRaises(PowerError):
            self.tencent(api).stop()
        self.assertNotIn("StopInstances", actions)

    def test_pack_id_allows_only_ascii_letters_digits_and_hyphen(self) -> None:
        self.assertEqual(validate_pack_id("Pack-01"), "Pack-01")
        for invalid in ("pack_name", "pack.name", "../pack", "中文"):
            with self.subTest(invalid=invalid), self.assertRaises(ModpackError):
                validate_pack_id(invalid)

    def test_webhook_uses_normalized_contract(self) -> None:
        replies = iter(({"state": "stopped"}, {}, {"state": "RUNNING", "public-ip": "203.0.113.2"}))
        calls: list[tuple[str, str, str, str, str]] = []

        def function(action: str):
            def call(instance: str, region: str, secret_id: str, secret_key: str):
                calls.append((action, instance, region, secret_id, secret_key))
                return next(replies)
            return call

        adapter = SimpleNamespace(**{action: function(action) for action in ("status", "start", "stop")})
        result = Power(
            adapter, PowerTarget("manager", "local"), "", "token",
            sleeper=lambda _: None,
        ).start()
        self.assertEqual(result, InstanceStatus("RUNNING", "203.0.113.2"))
        self.assertEqual([item[0] for item in calls], ["status", "start", "status"])
        self.assertEqual(calls[0], ("status", "manager", "local", "", "token"))


class DeploymentTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = self.root / "config.yml"
        self.config.write_text(yaml.safe_dump({
            "manager": {
                "host": "manager.example", "user": "minecraft", "dir": "/srv/mcsp/manager",
                "auto-stop": False, "stop-at": "23:05",
                "power-api": {
                    "provider": "tencent", "instance-region": "ap-test",
                    "instance-id": "ins-test",
                },
                "modpack": {"auto-stop": False, "java": [17], "mem-limit": "4g"},
            },
            "monitor": {"enabled": False}, "cos": {"enabled": False},
        }), encoding="utf-8")
        self.archive = self.root / "source.tar.gz"
        self.archive.write_bytes(b"archive")

    def test_manager_deploy_uploads_only_manager_section_and_preserves_runtime_data(self) -> None:
        transport = FakeTransport(self.archive)
        DeploymentService(self.config, transport=transport).deploy_manager()
        config_upload = next(data for destination, data in transport.uploads if destination.endswith("/config.yml"))
        deployed = yaml.safe_load(config_upload)
        self.assertIn("modpack", deployed)
        self.assertNotIn("monitor", deployed)
        scripts = "\n".join(" ".join(call[1]) for call in transport.ssh_calls)
        self.assertNotIn("/modpacks;", scripts)
        self.assertNotIn("/.schedule-state.json", scripts)
        self.assertIn("schedule install", scripts)
        self.assertNotIn("modpacks", transport.excluded)
        self.assertTrue(transport.cleaned)

    def test_pack_deploy_rejects_path_traversal_before_remote_access(self) -> None:
        transport = FakeTransport(self.archive)
        config = load_config(self.config)
        service = ModpackService(config, transport=transport)
        with self.assertRaisesRegex(RuntimeError, "无效整合包 ID"):
            service.deploy_pack("../escape")
        self.assertFalse(transport.ssh_calls)

    def test_cli_selects_custom_adapter_and_reads_fixed_env_key(self) -> None:
        document = yaml.safe_load(self.config.read_text(encoding="utf-8"))
        document["manager"]["power-api"] = {
            "provider": "xxxaaa", "instance-id": "manager",
            "instance-region": "local",
        }
        self.config.write_text(yaml.safe_dump(document), encoding="utf-8")
        (self.root / ".env").write_text("POWER_SECRET_KEY=secret\n", encoding="utf-8")
        with patch("lib.power.load_adapter", return_value=SimpleNamespace()):
            self.assertIsInstance(CliServices(self.config).power(), Power)

    def test_manager_deploy_selects_custom_adapter_secret(self) -> None:
        document = yaml.safe_load(self.config.read_text(encoding="utf-8"))
        document["manager"].update({
            "auto-stop": True,
            "power-api": {
                "provider": "xxxaaa", "instance-id": "manager",
                "instance-region": "local",
            },
        })
        document["manager"]["modpack"].update({"auto-stop": True, "stop-at": "23:00"})
        self.config.write_text(yaml.safe_dump(document), encoding="utf-8")
        (self.root / ".env").write_text("POWER_SECRET_KEY=secret\n", encoding="utf-8")
        service = DeploymentService(self.config, transport=FakeTransport(self.archive))
        self.assertEqual(
            service._manager_env(),
            "POWER_SECRET_ID=\nPOWER_SECRET_KEY=secret\n",
        )

    def test_shell_join_quotes_untrusted_console_text(self) -> None:
        self.assertEqual(shell_join(["console", "demo", "say $(id)"]), "console demo 'say $(id)'")

    def test_source_archive_keeps_template_but_excludes_local_pack(self) -> None:
        source = self.root / "manager-source"
        (source / "modpacks/template").mkdir(parents=True)
        (source / "modpacks/demo").mkdir(parents=True)
        (source / "modpacks/template/config.yml").write_text("template")
        (source / "modpacks/demo/config.yml").write_text("demo")
        archive = archive_tree(source, exclude={"modpacks/demo"})
        try:
            with tarfile.open(archive, "r:gz") as bundle:
                names = bundle.getnames()
            self.assertIn("modpacks/template/config.yml", names)
            self.assertNotIn("modpacks/demo/config.yml", names)
        finally:
            cleanup_archive(archive)


if __name__ == "__main__":
    unittest.main()
