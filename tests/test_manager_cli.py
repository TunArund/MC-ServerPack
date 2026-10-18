from __future__ import annotations

import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from manager.lib.cli import dispatch


class FakeManager:
    def __init__(self) -> None:
        self.call: tuple | None = None

    def logs(self, pack_id: str, *, lines: int, follow: bool) -> None:
        self.call = ("logs", pack_id, lines, follow)

    def console(self, pack_id: str, command: str) -> None:
        self.call = ("console", pack_id, command)

    def get_property(self, pack_id: str, key: str):
        self.call = ("get-property", pack_id, key)
        return "true", Path("/runtime/data/server.properties")

    def set_property(self, pack_id: str, key: str, value: str):
        self.call = ("set-property", pack_id, key, value)
        return Path("/runtime/data/server.properties"), True

    def list_packs(self):
        self.call = ("list",)
        spec = type("Spec", (), {
            "pack_id": "example", "port": 25565, "java": 17,
            "memory": "2g", "version": "1.20.1", "name": "Example",
        })()
        return [(spec, True)]


class ManagerCliTests(unittest.TestCase):
    def test_log_options_are_parsed_after_action(self) -> None:
        manager = FakeManager()
        self.assertEqual(
            dispatch(["logs", "example", "--lines", "42", "--follow"], manager), 0,
        )
        self.assertEqual(manager.call, ("logs", "example", 42, True))

    def test_console_preserves_arguments_as_text(self) -> None:
        manager = FakeManager()
        self.assertEqual(dispatch(["console", "example", "say", "hello"], manager), 0)
        self.assertEqual(manager.call, ("console", "example", "say hello"))

    def test_property_read_keeps_value_and_effective_path_on_summary_line(self) -> None:
        manager = FakeManager()
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(
                dispatch(["property", "example", "online-mode"], manager), 0,
            )
        self.assertEqual(manager.call, ("get-property", "example", "online-mode"))
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        self.assertIn("online-mode=true", output.getvalue())
        self.assertIn("/runtime/data/server.properties", output.getvalue())

    def test_property_write_reports_restart_when_running(self) -> None:
        manager = FakeManager()
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(dispatch([
                "property", "example", "online-mode", "false",
            ], manager), 0)
        self.assertEqual(
            manager.call, ("set-property", "example", "online-mode", "false"),
        )
        self.assertIn("请执行 ./manager.sh stop example", output.getvalue())

    def test_list_prints_pack_summary(self) -> None:
        manager = FakeManager()
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(dispatch(["list"], manager), 0)
        self.assertEqual(manager.call, ("list",))
        self.assertIn("ID\tSTATE\tPORT", output.getvalue())
        self.assertIn("example\trunning\t25565\t17\t2g\t1.20.1\tExample", output.getvalue())


if __name__ == "__main__":
    unittest.main()
