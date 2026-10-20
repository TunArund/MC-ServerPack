from __future__ import annotations

import ast
import io
import json
import tempfile
import unittest
import json
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, call, patch

from terminal.src.cli import CliServices, dispatch, main
from lib.power import InstanceStatus


class FakeServices:
    def __init__(self) -> None:
        self.calls = []

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

    def control(self, target: str, action: str, *, force: bool = False):
        self.calls.append(("control", target, action, force))
        if target == "manager":
            return InstanceStatus("STOPPED" if action == "stop" else "RUNNING")
        return None

    def manager_command(
        self, action: str, pack_id=None, *, extra=(), tty: bool = False,
    ) -> None:
        self.calls.append(("manager", action, pack_id, extra, tty))

    def local_manager(self, arguments: list[str]) -> None:
        self.calls.append(("local-manager", arguments))

    def monitor_command(self, action: str) -> None:
        self.calls.append(("monitor", action))

    def add_monitor_admin(self) -> str:
        self.calls.append(("add-admin",))
        return "openid"

    def reset_monitor(self) -> None:
        self.calls.append(("reset",))

    def reset_monitor_messages(self) -> None:
        self.calls.append(("reset-messages",))

    def account_context(self, target: str, key_path=None):
        host = f"{target}.example"
        source = key_path or Path("/home/operator/.ssh/id_ed25519.pub")
        return host, source

    def test_account_login(self, host: str, administrator: str, key_path) -> None:
        self.calls.append(("test-login", host, administrator, key_path))

    def create_user(
        self, host: str, username: str, administrator: str, key_path,
    ) -> None:
        self.calls.append(
            ("create-user", host, username, administrator, key_path)
        )

    def deployment_targets(self):
        return ("manager", "monitor")


class TerminalCliRedesignTests(unittest.TestCase):
    def test_json_mode_writes_one_stdout_document(self) -> None:
        output, error = io.StringIO(), io.StringIO()

        def fake_dispatch(_arguments):
            from terminal.src.output import renderer
            renderer.result('{"value":1}\n', data=True)
            return 0

        with (
            patch("terminal.src.cli.configure_logging"),
            patch("terminal.src.cli.dispatch", side_effect=fake_dispatch),
            redirect_stdout(output), redirect_stderr(error),
        ):
            self.assertEqual(main(["--json", "config", "check"]), 0)
        self.assertEqual(json.loads(output.getvalue()), {
            "ok": True, "result": {"value": 1},
        })
        self.assertEqual(error.getvalue(), "")

    def test_json_mode_formats_argument_errors(self) -> None:
        output, error = io.StringIO(), io.StringIO()
        with (
            patch("terminal.src.cli.configure_logging"),
            redirect_stdout(output), redirect_stderr(error),
            self.assertRaises(SystemExit) as raised,
        ):
            main(["--json", "unknown"])
        self.assertEqual(raised.exception.code, 2)
        self.assertFalse(error.getvalue())
        self.assertFalse(json.loads(output.getvalue())["ok"])

    def test_quiet_hides_progress_and_success_but_keeps_data(self) -> None:
        from terminal.src.output import Renderer
        output, error = io.StringIO(), io.StringIO()
        render = Renderer(quiet=True)
        with redirect_stdout(output), redirect_stderr(error):
            render.progress("progress")
            render.result("success")
            render.result("data", data=True)
            render.error("failure")
        self.assertEqual(output.getvalue(), "data\n")
        self.assertEqual(error.getvalue(), "failure\n")

    def test_backup_list_is_forwarded_without_terminal_business_logic(self) -> None:
        services = FakeServices()
        self.assertEqual(
            dispatch(["manager", "backup", "list", "example"], services), 0,
        )
        self.assertEqual(
            services.calls, [("manager", "backup", "list", ("example",), False)],
        )

    def test_restore_auto_restart_is_forwarded(self) -> None:
        services = FakeServices()
        self.assertEqual(dispatch([
            "manager", "restore", "example", "snapshot.tar.gz", "--auto-restart",
        ], services), 0)
        self.assertEqual(services.calls, [(
            "manager", "restore", "example",
            ("snapshot.tar.gz", "--auto-restart"), False,
        )])

    def test_main_audits_every_command_and_exit_code(self) -> None:
        with (
            patch("terminal.src.cli.audit") as audit,
            patch("terminal.src.cli.dispatch", return_value=0),
            patch("terminal.src.cli.operation_log", return_value=nullcontext()),
        ):
            self.assertEqual(main(["config", "check"]), 0)
        self.assertEqual(audit.call_args_list, [
            call("$ mcsp config check"), call("exit=0"),
        ])

    def test_main_records_structured_events_without_copying_stdout(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        log = root / "logs/terminal.log"

        def fake_dispatch(_arguments):
            print("部署阶段：已完成")
            return 0

        with (
            patch("terminal.src.cli.ROOT", root),
            patch("terminal.src.cli.TERMINAL_LOG", log),
            patch("terminal.src.cli.dispatch", side_effect=fake_dispatch),
        ):
            self.assertEqual(main(["deploy", "monitor"]), 0)

        events = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(events[0]["event"], "operation-started")
        self.assertEqual(events[0]["command"], ["mcsp", "deploy", "monitor"])
        self.assertEqual(events[-1]["event"], "operation-finished")
        self.assertNotIn("部署阶段：已完成", log.read_text(encoding="utf-8"))
        self.assertFalse((root / "logs/.operations").exists())
        self.assertFalse((root / "logs/.terminal-log.lock").exists())

    def test_config_init_creates_both_local_files(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        config = root / "config.yml"
        (root / "config.yml.example").write_text("manager: example\n")
        (root / ".env.example").write_text("TOKEN=\n")
        self.assertEqual(dispatch(["--config", str(config), "config", "init"]), 0)
        self.assertTrue(config.is_file())
        self.assertTrue((root / ".env").is_file())

    def test_deploy_test_checks_without_installing(self) -> None:
        services = FakeServices()
        self.assertEqual(dispatch(["deploy", "manager", "--test"], services), 0)
        self.assertEqual(services.calls, [("check", "manager")])

    def test_source_only_publishes_selected_runtime(self) -> None:
        services = FakeServices()
        self.assertEqual(dispatch(["deploy", "manager", "--source"], services), 0)
        self.assertEqual(services.calls, [("publish", "manager")])

    def test_config_only_updates_selected_runtime(self) -> None:
        services = FakeServices()
        self.assertEqual(dispatch(["deploy", "monitor", "--config"], services), 0)
        self.assertEqual(services.calls, [("configure", "monitor")])

    def test_docker_install_is_independent_from_upload(self) -> None:
        services = FakeServices()
        self.assertEqual(
            dispatch(["deploy", "manager", "--install-docker"], services), 0,
        )
        self.assertEqual(services.calls, [("install-docker", "manager")])

    def test_full_deploy_prepares_docker_before_runtime(self) -> None:
        services = CliServices.__new__(CliServices)
        operations = Mock()
        services.install_docker = operations.install_docker
        runtime = Mock()
        runtime.install.side_effect = operations.runtime_install
        services.manager_runtime = Mock(return_value=runtime)
        services.install("manager")
        self.assertEqual(operations.mock_calls, [
            call.install_docker("manager"),
            call.runtime_install(),
        ])

    def test_bare_deploy_installs_all_enabled_runtimes(self) -> None:
        services = FakeServices()
        self.assertEqual(dispatch(["deploy"], services), 0)
        self.assertEqual(services.calls, [
            ("install", "manager"), ("install", "monitor"),
        ])

    def test_deploy_test_all_checks_all_enabled_runtimes(self) -> None:
        services = FakeServices()
        self.assertEqual(dispatch(["deploy", "--test-all"], services), 0)
        self.assertEqual(services.calls, [
            ("check", "manager"), ("check", "monitor"),
        ])

    def test_each_runtime_can_create_its_configured_user(self) -> None:
        for target in ("manager", "monitor"):
            with self.subTest(target=target):
                services = FakeServices()
                key = Path("/tmp/operator.pub")
                with patch(
                    "builtins.input", side_effect=("root", "minecraft"),
                ) as prompt:
                    output = io.StringIO()
                    progress = io.StringIO()
                    with redirect_stdout(output), redirect_stderr(progress):
                        self.assertEqual(dispatch([
                            "deploy", target, "--create-user", "--key", str(key),
                        ], services), 0)
                self.assertEqual(prompt.call_count, 2)
                self.assertEqual(services.calls, [
                    ("test-login", f"{target}.example", "root", key),
                    ("create-user", f"{target}.example", "minecraft", "root", key),
                ])
                rendered = progress.getvalue()
                self.assertIn(
                    f"使用 {key.with_suffix('')} 私钥测试登录 "
                    f"root@{target}.example", rendered,
                )
                self.assertIn("将执行：", rendered)
                self.assertIn(
                    f"部署用户已就绪：minecraft@{target}.example",
                    output.getvalue(),
                )

    def test_create_user_blank_administrator_defaults_to_root(self) -> None:
        services = FakeServices()
        with patch("builtins.input", side_effect=("", "minecraft")):
            dispatch(["deploy", "manager", "--create-user"], services)
        key = Path("/home/operator/.ssh/id_ed25519.pub")
        self.assertEqual(services.calls, [
            ("test-login", "manager.example", "root", key),
            ("create-user", "manager.example", "minecraft", "root", key),
        ])

    def test_create_user_cannot_be_hidden_inside_deploy_test(self) -> None:
        services = FakeServices()
        with patch("builtins.input") as prompt, self.assertRaises(SystemExit):
            dispatch(
                ["deploy", "manager", "--test", "--create-user"], services,
            )
        prompt.assert_not_called()
        self.assertEqual(services.calls, [])

    def test_atomic_deploy_modes_are_mutually_exclusive(self) -> None:
        services = FakeServices()
        with self.assertRaises(SystemExit):
            dispatch([
                "deploy", "manager", "--source", "--install-docker",
            ], services)
        self.assertEqual(services.calls, [])

    def test_runtime_deploy_uses_public_install(self) -> None:
        services = FakeServices()
        dispatch(["deploy", "monitor"], services)
        self.assertEqual(services.calls, [("install", "monitor")])

    def test_modpack_deploy_is_a_single_adapter_call(self) -> None:
        services = FakeServices()
        dispatch(["deploy", "modpack", "Pack-01"], services)
        self.assertEqual(services.calls, [("deploy-modpack", "Pack-01")])

    def test_top_level_manager_target_controls_cloud_power(self) -> None:
        services = FakeServices()
        dispatch(["start", "manager"], services)
        self.assertEqual(services.calls, [("control", "manager", "start", False)])

    def test_top_level_monitor_target_controls_container(self) -> None:
        services = FakeServices()
        dispatch(["stop", "monitor"], services)
        self.assertEqual(services.calls, [("control", "monitor", "stop", False)])

    def test_manager_stop_requires_confirmation(self) -> None:
        services = FakeServices()
        with patch("builtins.input", return_value="no"):
            dispatch(["stop", "manager"], services)
        self.assertEqual(services.calls, [])

    def test_manager_namespace_calls_remote_manager(self) -> None:
        services = FakeServices()
        dispatch(["manager", "logs", "Pack-01", "--lines", "20"], services)
        self.assertEqual(services.calls, [
            ("manager", "logs", "Pack-01", ("--lines", "20"), False),
        ])

    def test_manager_create_force_is_forwarded_to_remote_manager(self) -> None:
        services = FakeServices()
        dispatch(["manager", "create", "Server-01", "--force"], services)
        self.assertEqual(services.calls, [(
            "manager", "create", "Server-01", ("--force",), False,
        )])

    def test_local_manager_arguments_are_forwarded_transparently(self) -> None:
        services = FakeServices()
        dispatch(["local", "manager", "remove", "Server-01", "--force"], services)
        self.assertEqual(services.calls, [(
            "local-manager", ["remove", "Server-01", "--force"],
        )])

    def test_manager_start_is_forwarded_without_terminal_business_flags(self) -> None:
        services = FakeServices()
        dispatch(["manager", "start", "Server-01"], services)
        self.assertEqual(services.calls, [(
            "manager", "start", "Server-01", (), False,
        )])

    def test_manager_property_is_forwarded_to_remote_manager(self) -> None:
        services = FakeServices()
        dispatch([
            "manager", "property", "Server-01", "online-mode", "false",
        ], services)
        self.assertEqual(services.calls, [(
            "manager", "property", "Server-01",
            ("online-mode", "false"), False,
        )])

    def test_manager_list_is_forwarded_without_pack_id(self) -> None:
        services = FakeServices()
        dispatch(["manager", "list"], services)
        self.assertEqual(services.calls, [(
            "manager", "list", None, (), False,
        )])

    def test_local_namespace_is_explicit(self) -> None:
        services = FakeServices()
        dispatch(["local", "manager", "create", "Pack-01"], services)
        self.assertEqual(services.calls, [(
            "local-manager", ["create", "Pack-01"],
        )])

    def test_monitor_namespace_calls_remote_monitor(self) -> None:
        services = FakeServices()
        dispatch(["monitor", "status"], services)
        self.assertEqual(services.calls, [("monitor", "status")])

    def test_monitor_reset_calls_runtime_reset(self) -> None:
        services = FakeServices()
        dispatch(["monitor", "reset-targets"], services)
        self.assertEqual(services.calls, [("reset",)])

    def test_monitor_message_reset_calls_runtime_reset(self) -> None:
        services = FakeServices()
        dispatch(["monitor", "reset-messages"], services)
        self.assertEqual(services.calls, [("reset-messages",)])


    def test_cli_does_not_import_runtime_implementation_modules(self) -> None:
        source = Path("terminal/src/cli.py").read_text(encoding="utf-8")
        imports = {
            alias.name
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        self.assertFalse({"docker", "subprocess", "tarfile"} & imports)


if __name__ == "__main__":
    unittest.main()
