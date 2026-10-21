from __future__ import annotations

import tempfile
import unittest
import logging
import logging.handlers
import json
import io
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch
from pathlib import Path

from lib.observability_sdk import REDACTED, configure_logging, redact_argv
from manager.src.jobs import Job
from manager.src.job_bridge import main as bridge_main


ROOT = Path(__file__).resolve().parents[2]


class OutputContractTests(unittest.TestCase):
    def test_command_arguments_are_redacted_before_logging(self) -> None:
        self.assertEqual(
            redact_argv([
                "deploy", "--api-key", "secret", "TOKEN=value",
                "--password=hunter2", "manager",
            ]),
            [
                "deploy", "--api-key", REDACTED, f"TOKEN={REDACTED}",
                f"--password={REDACTED}", "manager",
            ],
        )

    def test_job_events_have_stable_type_and_code(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            job = Job(Path(raw), "request", "start")
            self.assertEqual(job.events[0]["type"], "progress")
            self.assertEqual(job.events[0]["code"], "job-queued")
            job.emit(
                "health-check", 1, 2, "等待健康",
                code="minecraft-health-waiting",
            )
            self.assertEqual(job.events[-1]["code"], "minecraft-health-waiting")

    def test_structured_log_fields_are_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            log = Path(raw) / "service.log"
            configure_logging({"": log})
            logging.getLogger("test").info(
                "request", extra={
                    "api_key": "secret",
                    "detail": {"access_token": "token", "status": "ok"},
                },
            )
            event = json.loads(log.read_text(encoding="utf-8"))
            self.assertEqual(event["api_key"], REDACTED)
            self.assertEqual(event["detail"]["access_token"], REDACTED)
            self.assertEqual(event["detail"]["status"], "ok")

    def test_terminal_uses_standard_rotating_file_handler(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            log = Path(raw) / "terminal.log"
            configure_logging({"": log})
            handlers = logging.getLogger().handlers
            self.assertEqual(len(handlers), 1)
            self.assertIsInstance(handlers[0], logging.handlers.RotatingFileHandler)
            self.assertEqual(handlers[0].maxBytes, 5 * 1024 * 1024)
            self.assertEqual(handlers[0].backupCount, 3)
            logging.getLogger("test").info("works")
            self.assertTrue(log.is_file())
            self.assertFalse((Path(raw) / ".operations").exists())
            self.assertFalse((Path(raw) / ".terminal-log.lock").exists())

    def test_each_runtime_has_one_documented_log_source(self) -> None:
        manager = (ROOT / "manager/src/app.py").read_text(encoding="utf-8")
        monitor = (ROOT / "monitor/monitor.sh").read_text(encoding="utf-8")
        self.assertIn('"manager": directory / "service.log"', manager)
        self.assertIn('"manager.api": directory / "api.log"', manager)
        self.assertNotIn('"manager.log"', manager)
        self.assertNotIn('"schedule.log"', manager)
        self.assertNotIn("monitor.log", monitor)

    def test_manager_shell_bridge_parses_and_renders_job_json(self) -> None:
        document = {"ok": True, "data": {
            "state": "running",
            "events": [{
                "seq": 3, "stage": "start", "current": 1, "total": 2,
                "message": "包含引号 \" 与换行\n也能解析", "detail": {},
            }],
        }}
        output, error = io.StringIO(), io.StringIO()
        with (
            patch("sys.stdin", io.StringIO(json.dumps(document))),
            redirect_stdout(output), redirect_stderr(error),
        ):
            self.assertEqual(bridge_main(["job-update", "0"]), 0)
        self.assertEqual(output.getvalue(), "running\t3\n")
        self.assertIn('包含引号 " 与换行\n也能解析', error.getvalue())

    def test_manager_shell_does_not_parse_json_with_text_tools(self) -> None:
        source = (ROOT / "manager/manager.sh").read_text(encoding="utf-8")
        self.assertNotIn("grep -o", source)
        self.assertNotIn("sed -n", source)
        self.assertIn("src.job_bridge", source)


if __name__ == "__main__":
    unittest.main()
