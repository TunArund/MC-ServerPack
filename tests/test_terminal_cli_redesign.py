from __future__ import annotations

import ast
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, call, patch

from lib.cli import CliServices, dispatch, make_parser
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

    def deploy_modpack(self, pack_id: str, *, force: bool = False) -> None:
        self.calls.append(("deploy-modpack", pack_id, force))

    def install_docker(self, target: str) -> None:
        self.calls.append(("install-docker", target))

    def install_python(self) -> None:
        self.calls.append(("install-python", "manager"))

    def control(self, target: str, action: str, *, force: bool = False):
        self.calls.append(("control", target, action, force))
        if target == "manager":
            return InstanceStatus("STOPPED" if action == "stop" else "RUNNING")
        return None

    def manager_command(
        self, action: str, pack_id=None, *, extra=(), tty: bool = False,
    ) -> None:
        self.calls.append(("manager", action, pack_id, extra, tty))

    def local_create(self, pack_id: str, *, extra=()) -> Path:
        self.calls.append(("local-create", pack_id, extra))
        return Path("manager/modpacks") / pack_id

    def monitor_command(self, action: str) -> None:
        self.calls.append(("monitor", action))

    def add_monitor_target(self, name: str) -> str:
        self.calls.append(("add-target", name))
        return "openid"

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

    def test_python_install_is_independent_from_source_upload(self) -> None:
        services = FakeServices()
        self.assertEqual(
            dispatch(["deploy", "manager", "--install-python"], services), 0,
        )
        self.assertEqual(services.calls, [("install-python", "manager")])

    def test_source_full_deploy_prepares_docker_and_python_before_runtime(self) -> None:
        services = CliServices.__new__(CliServices)
        services.source_mode = True
        operations = Mock()
        services.install_docker = operations.install_docker
        services.install_python = operations.install_python
        runtime = Mock()
        runtime.install.side_effect = operations.runtime_install
        services.manager_runtime = Mock(return_value=runtime)
        services.install("manager")
        self.assertEqual(operations.mock_calls, [
            call.install_docker("manager"),
            call.install_python(),
            call.runtime_install(),
        ])

    def test_packaged_full_deploy_does_not_prepare_python(self) -> None:
        services = CliServices.__new__(CliServices)
        services.source_mode = False
        services.install_docker = Mock()
        services.install_python = Mock()
        runtime = Mock()
        services.manager_runtime = Mock(return_value=runtime)
        services.install("manager")
        services.install_docker.assert_called_once_with("manager")
        services.install_python.assert_not_called()
        runtime.install.assert_called_once_with()
        with self.assertRaises(SystemExit):
            make_parser(source_mode=False).parse_args([
                "deploy", "manager", "--install-python",
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
                    with redirect_stdout(output):
                        self.assertEqual(dispatch([
                            "deploy", target, "--create-user", "--key", str(key),
                        ], services), 0)
                self.assertEqual(prompt.call_count, 2)
                self.assertEqual(services.calls, [
                    ("test-login", f"{target}.example", "root", key),
                    ("create-user", f"{target}.example", "minecraft", "root", key),
                ])
                rendered = output.getvalue()
                self.assertIn(
                    f"使用 {key.with_suffix('')} 私钥测试登录 "
                    f"root@{target}.example", rendered,
                )
                self.assertIn("将执行：", rendered)
                self.assertIn(
                    f"部署用户已就绪：minecraft@{target}.example", rendered,
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

    def test_old_cross_runtime_config_command_is_removed(self) -> None:
        services = FakeServices()
        with self.assertRaises(SystemExit):
            dispatch(["deploy", "config", "manager"], services)
        self.assertEqual(services.calls, [])

    def test_old_upload_flag_is_removed(self) -> None:
        services = FakeServices()
        with self.assertRaises(SystemExit):
            dispatch(["deploy", "manager", "--upload"], services)
        self.assertEqual(services.calls, [])

    def test_modpack_deploy_is_a_single_adapter_call(self) -> None:
        services = FakeServices()
        dispatch(["deploy", "modpack", "Pack-01", "--force"], services)
        self.assertEqual(services.calls, [("deploy-modpack", "Pack-01", True)])

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
        dispatch(["manager", "logs", "Pack-01", "--follow"], services)
        self.assertEqual(services.calls, [
            ("manager", "logs", "Pack-01", ("--lines", "100", "--follow"), True),
        ])

    def test_manager_vanilla_create_is_forwarded_to_remote_manager(self) -> None:
        services = FakeServices()
        dispatch([
            "manager", "create", "Vanilla-01", "--vanilla", "1.21.10",
            "--accept-eula",
        ], services)
        self.assertEqual(services.calls, [(
            "manager", "create", "Vanilla-01",
            ("--vanilla", "1.21.10", "--accept-eula"), False,
        )])

    def test_local_vanilla_create_is_forwarded_to_local_manager(self) -> None:
        services = FakeServices()
        dispatch([
            "local", "manager", "create", "Vanilla-01", "--vanilla",
            "--accept-eula", "--force",
        ], services)
        self.assertEqual(services.calls, [(
            "local-create", "Vanilla-01",
            ("--vanilla", "latest", "--accept-eula", "--force"),
        )])

    def test_local_namespace_is_explicit(self) -> None:
        services = FakeServices()
        output = io.StringIO()
        with redirect_stdout(output):
            dispatch(["local", "manager", "create", "Pack-01"], services)
        self.assertEqual(services.calls, [("local-create", "Pack-01", ())])
        self.assertIn("请覆盖 src/ 内容：", output.getvalue())
        self.assertIn("请调整 config.yml 配置：", output.getvalue())

    def test_monitor_namespace_calls_remote_monitor(self) -> None:
        services = FakeServices()
        dispatch(["monitor", "status"], services)
        self.assertEqual(services.calls, [("monitor", "status")])

    def test_cli_does_not_import_runtime_implementation_modules(self) -> None:
        source = Path("lib/cli.py").read_text(encoding="utf-8")
        imports = {
            alias.name
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        self.assertFalse({"docker", "subprocess", "tarfile"} & imports)


if __name__ == "__main__":
    unittest.main()
