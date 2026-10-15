from __future__ import annotations

import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

import yaml

from lib.cli import dispatch
from lib.cloud import CloudError, CloudTarget, InstanceStatus, TencentCloud
from lib.remote import archive_tree, cleanup_archive, shell_join
from lib.terminal import TerminalService


class FakeCloud:
    def __init__(self) -> None:
        self.calls: list[object] = []

    def status(self):
        self.calls.append("status")
        return InstanceStatus("RUNNING", "203.0.113.1")

    def start(self):
        self.calls.append("start")
        return InstanceStatus("RUNNING")

    def stop(self, *, continue_on_stop_failure: bool):
        self.calls.append(("stop", continue_on_stop_failure))
        return InstanceStatus("STOPPED")


class FakeService:
    def __init__(self) -> None:
        self.calls: list[object] = []
        self.power = FakeCloud()

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

    def cloud(self):
        return self.power


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
        dispatch(["manager", "stop", "--force"], service)
        self.assertEqual(service.power.calls, [("stop", True)])


class CloudTests(unittest.TestCase):
    def target(self) -> CloudTarget:
        return CloudTarget("ap-test", "ins-test", "/srv/mcsp", "minecraft")

    def test_start_is_idempotent_and_waits_for_running(self) -> None:
        states = iter(("STOPPED", "RUNNING"))
        actions: list[str] = []

        def api(service, action, payload):
            actions.append(action)
            if action == "DescribeInstances":
                return {"InstanceSet": [{"InstanceId": "ins-test", "InstanceState": next(states)}]}
            return {}

        result = TencentCloud(self.target(), "id", "key", api_call=api, sleeper=lambda _: None).start()
        self.assertEqual(result.state, "RUNNING")
        self.assertEqual(actions, ["DescribeInstances", "StartInstances", "DescribeInstances"])

    def test_stop_runs_tat_before_soft_poweroff(self) -> None:
        descriptions = iter(("RUNNING", "STOPPED"))
        actions: list[str] = []

        def api(service, action, payload):
            actions.append(action)
            if action == "DescribeInstances":
                return {"InstanceSet": [{"InstanceId": "ins-test", "InstanceState": next(descriptions)}]}
            if action == "RunCommand":
                return {"InvocationId": "inv-test"}
            if action == "DescribeInvocationTasks":
                return {"InvocationTaskSet": [{
                    "InstanceId": "ins-test", "TaskStatus": "SUCCESS",
                    "TaskResult": {"ExitCode": 0},
                }]}
            return {}

        result = TencentCloud(self.target(), "id", "key", api_call=api, sleeper=lambda _: None).stop()
        self.assertEqual(result.state, "STOPPED")
        self.assertEqual(actions, [
            "DescribeInstances", "RunCommand", "DescribeInvocationTasks",
            "StopInstances", "DescribeInstances",
        ])

    def test_stop_does_not_poweroff_after_tat_failure_by_default(self) -> None:
        actions: list[str] = []

        def api(service, action, payload):
            actions.append(action)
            if action == "DescribeInstances":
                return {"InstanceSet": [{"InstanceId": "ins-test", "InstanceState": "RUNNING"}]}
            if action == "RunCommand":
                raise RuntimeError("denied")
            return {}

        with self.assertRaises(CloudError):
            TencentCloud(self.target(), "id", "key", api_call=api).stop()
        self.assertNotIn("StopInstances", actions)


class DeploymentTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = self.root / "config.yml"
        self.config.write_text(yaml.safe_dump({
            "manager": {
                "host": "manager.example", "user": "minecraft", "dir": "/srv/mcsp/manager",
                "auto-stop": False, "stop-at": "23:05", "region": "ap-test",
                "instance-id": "ins-test", "modpack": {"auto-stop": False, "java": [17], "mem-limit": "4g"},
            },
            "monitor": {"enabled": False}, "cos": {"enabled": False},
        }), encoding="utf-8")
        self.archive = self.root / "source.tar.gz"
        self.archive.write_bytes(b"archive")

    def test_manager_deploy_uploads_only_manager_section_and_preserves_runtime_data(self) -> None:
        transport = FakeTransport(self.archive)
        TerminalService(self.config, transport=transport).deploy_manager()
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
        service = TerminalService(self.config, transport=transport)
        with self.assertRaisesRegex(RuntimeError, "无效整合包 ID"):
            service.deploy_pack("../escape")
        self.assertFalse(transport.ssh_calls)

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
