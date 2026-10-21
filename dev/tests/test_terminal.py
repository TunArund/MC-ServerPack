from __future__ import annotations

import io
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import yaml

from terminal.src.cli import CliServices, dispatch
from terminal.src.config import load as load_config
from terminal.src.deploy import RuntimeDeployment
from terminal.src.manager import Manager
from terminal.src.monitor import Monitor
from lib.power_sdk import InstanceStatus, Power, PowerError, PowerTarget
from lib.providers import tencent
from terminal.src.remote import CommandError, fit_terminal, shell_join
from manager.src.config import ConfigError as ManagerConfigError, validate_pack_id


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


class TerminalRenderingTests(unittest.TestCase):
    def test_terminal_truncation_counts_wide_characters(self) -> None:
        self.assertEqual(fit_terminal("启动 abc", 6), "启动 a")

    def test_terminal_truncation_removes_ansi_control_sequences(self) -> None:
        self.assertEqual(fit_terminal("\x1b[31merror\x1b[0m", 5), "error")


class FakeService:
    def __init__(self) -> None:
        self.calls: list[object] = []
        self.cloud = FakeCloud()
        self.stop_error: Exception | None = None

    def check(self, target: str) -> None:
        self.calls.append(("check", target))

    def install(self, target: str) -> None:
        self.calls.append(("install", target))

    def publish(self, target: str) -> None:
        self.calls.append(("publish", target))

    def configure(self, target: str) -> None:
        self.calls.append(("configure", target))

    def deploy_modpack(self, pack_id: str) -> None:
        self.calls.append(("deploy-modpack", pack_id))

    def install_docker(self, target: str) -> None:
        self.calls.append(("install-docker", target))

    def local_manager(self, arguments: list[str]) -> None:
        self.calls.append(("local-manager", arguments))

    def manager_command(
        self, action: str, pack_id: str | None = None, *,
        extra: tuple[str, ...] = (), tty: bool = False,
    ) -> None:
        self.calls.append(("manager", action, pack_id, extra, tty))

    def control(self, target: str, action: str, *, force: bool = False):
        self.calls.append(("control", target, action, force))
        if self.stop_error:
            raise self.stop_error
        return None if target == "monitor" else InstanceStatus(
            "STOPPED" if action == "stop" else "RUNNING"
        )

    def add_monitor_admin(self) -> str:
        self.calls.append(("add-monitor-admin",))
        return "user-openid"

    def reset_monitor(self) -> None:
        self.calls.append(("reset-monitor",))


class FakeTransport:
    def __init__(self, archive: Path) -> None:
        self.archive_path = archive
        self.ssh_calls: list[tuple[str, list[str], dict]] = []
        self.uploads: list[tuple[str, bytes]] = []
        self.ssh_stdout: bytes = b""

    def ssh(self, target: str, arguments: list[str], **kwargs):
        self.ssh_calls.append((target, arguments, kwargs))
        return subprocess.CompletedProcess(
            arguments,
            1 if arguments[:2] == ["test", "-e"] else 0,
            stdout=self.ssh_stdout,
        )

    def ssh_logged(self, target: str, arguments: list[str], **kwargs):
        return self.ssh(target, arguments, **kwargs)

    def ssh_tail(self, target: str, arguments: list[str], **kwargs):
        return self.ssh(target, arguments, **kwargs)

    def scp(self, source: Path, destination: str) -> None:
        self.uploads.append((destination, source.read_bytes()))

    def upload_file(self, source: Path, destination: str, remote_path: str, *, mode=None):
        self.uploads.append((f"{destination}:{remote_path}", source.read_bytes()))
        return True

    def sync_tree(
        self, source: Path, destination: str, remote_root: str, *,
        preserve=(), exclude=(),
    ):
        return 1, 0

class TerminalCliTests(unittest.TestCase):
    def test_monitor_add_admin(self) -> None:
        service = FakeService()
        self.assertEqual(dispatch(["monitor", "add-admin"], service), 0)
        self.assertEqual(service.calls, [("add-monitor-admin",)])

    def test_monitor_reset(self) -> None:
        service = FakeService()
        self.assertEqual(dispatch(["monitor", "reset-targets"], service), 0)
        self.assertEqual(service.calls, [("reset-monitor",)])

    def test_monitor_reset_uses_remote_force_flag(self) -> None:
        services = Monitor.__new__(Monitor)
        services.command = Mock(return_value=subprocess.CompletedProcess([], 0))
        services.reset()
        services.command.assert_called_once_with(["reset-targets", "--force"])

    def test_monitor_message_reset_uses_remote_force_flag(self) -> None:
        services = Monitor.__new__(Monitor)
        services.command = Mock(return_value=subprocess.CompletedProcess([], 0))
        services.reset_messages()
        services.command.assert_called_once_with(["reset-messages", "--force"])

    def test_monitor_check_uses_public_check_command(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        config_path = Path(temporary.name) / "config.yml"
        document = yaml.safe_load(
            (Path(__file__).resolve().parents[2] / "terminal/config.yml.example").read_text(
                encoding="utf-8",
            )
        )
        document["monitor"]["enabled"] = True
        document["monitor"]["host"] = "monitor.example"
        config_path.write_text(yaml.safe_dump(document), encoding="utf-8")
        transport = FakeTransport(Path(temporary.name) / "unused.tar.gz")
        service = Monitor(config_path, transport=transport)
        service.check()
        self.assertTrue(any(
            call[1] == ["bash", "-s", "--", "check"]
            for call in transport.ssh_calls
        ))

    def test_monitor_add_admin_uses_remote_verification(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        config_path = Path(temporary.name) / "config.yml"
        config_path.write_text(
            (Path(__file__).resolve().parents[2] / "terminal/config.yml.example").read_text(),
            encoding="utf-8",
        )
        (config_path.parent / ".env").write_text(
            "POWER_SECRET_ID=id\nPOWER_SECRET_KEY=key\n"
            "QQBOT_APP_ID=bot\nQQBOT_APP_KEY=token\n",
            encoding="utf-8",
        )
        services = Monitor.__new__(Monitor)
        services.config_path = config_path
        services.deployment = Mock()
        services.command = Mock(return_value=subprocess.CompletedProcess(
            [], 0, stdout=b"admin_openid: new-openid\n",
        ))
        with patch("terminal.src.monitor.secrets.token_hex", return_value="a1b2"):
            result = services.add_admin()
        self.assertEqual(result, "new-openid")
        services.command.assert_any_call(
            ["add-admin", "a1b2"], capture=True,
        )
        services.deployment.configure.assert_not_called()

    def test_pack_arguments_are_forwarded_without_shell_joining(self) -> None:
        service = FakeService()
        self.assertEqual(dispatch(["manager", "console", "demo", "say", "hello world"], service), 0)
        self.assertEqual(service.calls, [
            ("manager", "console", "demo", ("say", "hello world"), False),
        ])

    def test_logs_forwards_lines_without_tty(self) -> None:
        service = FakeService()
        dispatch(["manager", "logs", "demo", "--lines", "20"], service)
        self.assertEqual(service.calls, [
            ("manager", "logs", "demo", ("--lines", "20"), False),
        ])

    def test_manager_list_uses_api_response(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        config = Path(temporary.name) / "config.yml"
        config.write_text(
            (Path(__file__).resolve().parents[2] / "terminal/config.yml.example").read_text(),
            encoding="utf-8",
        )
        transport = FakeTransport(Path(temporary.name) / "unused.tar.gz")
        service = Manager(config, transport=transport)
        output = io.StringIO()
        with patch.object(service, "_api_request", return_value={"modpacks": [
            {"id": "alpha", "state": "running"}, {"id": "beta", "state": "stopped"},
        ]}), redirect_stdout(output):
            service.run("list")
        self.assertIn('"alpha"', output.getvalue())
        self.assertFalse(transport.ssh_calls)

    def test_backup_list_uses_api_response(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        config = Path(temporary.name) / "config.yml"
        config.write_text(
            (Path(__file__).resolve().parents[2] / "terminal/config.yml.example").read_text(),
            encoding="utf-8",
        )
        transport = FakeTransport(Path(temporary.name) / "unused.tar.gz")
        service = Manager(config, transport=transport)
        output = io.StringIO()
        with patch.object(service, "_api_request", return_value={"backups": [
            {"id": "a1b2", "created_at": "now", "size": 2, "name": "backup.tar.gz"},
        ]}), redirect_stdout(output):
            service.run("backup", "list", extra=("example",))
        self.assertIn('"a1b2"', output.getvalue())
        self.assertFalse(transport.ssh_calls)

    def test_force_poweroff_is_explicit(self) -> None:
        service = FakeService()
        dispatch(["stop", "manager", "--force"], service)
        self.assertEqual(service.calls, [("control", "manager", "stop", True)])

    def test_stop_failure_prevents_poweroff_by_default(self) -> None:
        service = FakeService()
        service.stop_error = CommandError("SSH failed")
        with patch("builtins.input", return_value="yes"), self.assertRaises(CommandError):
            dispatch(["stop", "manager"], service)
        self.assertEqual(service.calls, [("control", "manager", "stop", False)])

    def test_pack_id_is_forwarded_to_manager_service(self) -> None:
        service = FakeService()
        dispatch(["manager", "status", "bad_name"], service)
        self.assertEqual(
            service.calls,
            [("manager", "status", "bad_name", (), False)],
        )


class PowerTests(unittest.TestCase):
    def target(self) -> PowerTarget:
        return PowerTarget("ins-test")

    def tencent(self, api, *, sleeper=lambda _: None) -> Power:
        patcher = patch.object(tencent, "_create_api", return_value=api)
        patcher.start()
        self.addCleanup(patcher.stop)
        return Power(tencent, self.target(), "id", "key", sleeper=sleeper)

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
            with self.subTest(invalid=invalid), self.assertRaises(ManagerConfigError):
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
                "timezone": "Asia/Shanghai",
                "auto-stop": False, "stop-at": "23:05",
                "power-api": {
                    "provider": "tencent", "instance-region": "ap-test",
                    "instance-id": "ins-test",
                },
                "modpack": {"auto-stop": False, "java": [17], "mem-limit": "4g"},
            },
            "monitor": {"enabled": False, "timezone": "Asia/Shanghai"},
            "cos": {"enabled": False},
        }), encoding="utf-8")
        (self.root / ".env").write_text("", encoding="utf-8")
        self.archive = self.root / "source.tar.gz"
        self.archive.write_bytes(b"archive")

    def test_manager_deploy_uploads_only_manager_section_and_preserves_runtime_data(self) -> None:
        service = Manager(self.config, transport=FakeTransport(self.archive))
        service.deployment = Mock()
        with patch.object(service, "_fetch_bootstrap"):
            service.install()
        config_text, environment = service.deployment.install.call_args.args
        deployed = yaml.safe_load(config_text)
        self.assertIn("modpack", deployed)
        self.assertNotIn("monitor", deployed)
        self.assertEqual(environment, "")

    def test_manager_bootstrap_fetch_stores_key_and_optional_ca(self) -> None:
        class Deployment:
            def __init__(self, with_ca: bool) -> None:
                self.with_ca = with_ca

            def download_path(self, name, local_path, *, mode=0o600, optional=False):
                if name == "data/.env":
                    local_path.write_text(
                        "MANAGER_API_KEY=" + "k" * 48 + "\n",
                        encoding="utf-8",
                    )
                    return True
                if name == "data/tls/ca.crt":
                    if not self.with_ca:
                        return False
                    local_path.write_text("ca\n", encoding="utf-8")
                    return True
                raise AssertionError(name)

        service = Manager(self.config, transport=FakeTransport(self.archive))
        service.tls_directory = self.root / "tls"
        service.tls_directory.mkdir()
        stale = service.tls_directory / "ca.crt"
        stale.write_text("stale\n", encoding="utf-8")
        service.deployment = Deployment(with_ca=False)
        service._fetch_bootstrap()
        self.assertIn(
            "MANAGER_API_KEY=" + "k" * 48,
            (self.root / ".env").read_text(encoding="utf-8"),
        )
        self.assertFalse(stale.exists())

        service.deployment = Deployment(with_ca=True)
        service._fetch_bootstrap()
        self.assertEqual(stale.read_text(encoding="utf-8"), "ca\n")

    def test_manager_config_sync_includes_filtered_environment(self) -> None:
        (self.root / ".env").write_text(
            "POWER_SECRET_ID=id\nPOWER_SECRET_KEY=key\n"
            "QQBOT_APP_KEY=not-for-manager\n",
            encoding="utf-8",
        )
        service = Manager(self.config, transport=FakeTransport(self.archive))
        service.deployment = Mock()
        with patch.object(service, "_fetch_bootstrap"):
            service.configure()
        config_text, environment = service.deployment.configure.call_args.args
        self.assertIn("modpack:", config_text)
        self.assertEqual(
            environment,
            "POWER_SECRET_ID=id\nPOWER_SECRET_KEY=key\n",
        )

    def test_generic_deployer_only_invokes_runtime_check_and_install(self) -> None:
        source = self.root / "runtime"
        source.mkdir()
        (source / "runtime.sh").write_text("#!/bin/sh\n", encoding="utf-8")
        transport = FakeTransport(self.archive)
        endpoint = load_config(self.config).manager.endpoint
        deployment = RuntimeDeployment(
            source, endpoint, "runtime.sh", transport=transport,
        )
        deployment.install("value: 1\n", "TOKEN=value\n")
        scripts = "\n".join(
            str(call[2].get("input_text", "")) + " " + " ".join(call[1])
            for call in transport.ssh_calls
        )
        self.assertNotIn("apt-get", scripts)
        self.assertNotIn("pip install", scripts)
        self.assertIn("check", scripts)
        self.assertIn("install", scripts)

    def test_monitor_admin_discovery_parses_remote_openid(self) -> None:
        document = yaml.safe_load(self.config.read_text(encoding="utf-8"))
        document["monitor"] = {
            "enabled": True,
            "host": "monitor.example",
            "user": "monitor",
            "dir": "/srv/mcsp/monitor",
            "timezone": "Asia/Shanghai",
            "manager": {
                "power-api": {
                    "provider": "tencent",
                    "instance-region": "ap-test",
                    "instance-id": "ins-test",
                },
                "api": {"host": "manager.example", "port": 27016},
            },
        }
        self.config.write_text(yaml.safe_dump(document), encoding="utf-8")
        transport = FakeTransport(self.archive)
        transport.ssh_stdout = b"admin_openid: discovered-user\n"
        service = Monitor(self.config, transport=transport)
        self.assertEqual(service.add_admin(), "discovered-user")
        command = " ".join(transport.ssh_calls[-1][1])
        self.assertIn("./monitor.sh add-admin", command)

    def test_monitor_deploy_uses_qqbot_app_environment_names(self) -> None:
        (self.root / ".env").write_text(
            "QQBOT_APP_ID=legacy-id\nQQBOT_APP_KEY=legacy-key\n",
            encoding="utf-8",
        )
        service = Monitor.__new__(Monitor)
        service.config_path = self.config
        self.assertEqual(
            service._environment(),
            "QQBOT_APP_ID=legacy-id\nQQBOT_APP_KEY=legacy-key\n",
        )

    def test_pack_deploy_rejects_path_traversal_before_remote_access(self) -> None:
        transport = FakeTransport(self.archive)
        service = Manager(self.config, transport=transport)
        with self.assertRaisesRegex(CommandError, "整合包 ID"):
            service.deploy_modpack("../escape")
        self.assertFalse(transport.ssh_calls)

    def test_cli_selects_custom_adapter_and_reads_fixed_env_key(self) -> None:
        document = yaml.safe_load(self.config.read_text(encoding="utf-8"))
        document["manager"]["power-api"] = {
            "provider": "xxxaaa", "instance-id": "manager",
            "instance-region": "local",
        }
        self.config.write_text(yaml.safe_dump(document), encoding="utf-8")
        (self.root / ".env").write_text("POWER_SECRET_KEY=secret\n", encoding="utf-8")
        with patch("terminal.src.manager.create_power", return_value=Mock(spec=Power)):
            self.assertIsInstance(Manager(self.config).power(), Power)

    def test_manager_service_owns_stop_then_poweroff_sequence(self) -> None:
        service = CliServices.__new__(CliServices)
        service.manager = Mock()
        power = FakeCloud()
        service.manager.power.return_value = power
        result = service.control("manager", "stop")
        self.assertEqual(result.state, "STOPPED")
        service.manager.run.assert_called_once_with("stop-all")
        self.assertEqual(power.calls, ["status", ("stop", "RUNNING")])

    def test_manager_service_force_allows_stop_failure(self) -> None:
        service = CliServices.__new__(CliServices)
        service.manager = Mock()
        service.manager.run.side_effect = CommandError("SSH failed")
        power = FakeCloud()
        service.manager.power.return_value = power
        service.control("manager", "stop", force=True)
        self.assertEqual(power.calls, ["status", ("stop", "RUNNING")])

    def test_manager_service_stop_failure_prevents_poweroff_by_default(self) -> None:
        service = CliServices.__new__(CliServices)
        service.manager = Mock()
        service.manager.run.side_effect = CommandError("SSH failed")
        power = FakeCloud()
        service.manager.power.return_value = power
        with self.assertRaises(CommandError):
            service.control("manager", "stop")
        self.assertEqual(power.calls, ["status"])

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
        service = Manager(self.config, transport=FakeTransport(self.archive))
        self.assertEqual(
            service._environment(),
            "POWER_SECRET_KEY=secret\n",
        )

    def test_shell_join_quotes_untrusted_console_text(self) -> None:
        self.assertEqual(shell_join(["console", "demo", "say $(id)"]), "console demo 'say $(id)'")

if __name__ == "__main__":
    unittest.main()
