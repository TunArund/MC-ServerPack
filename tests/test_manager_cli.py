from __future__ import annotations

import unittest

from manager.lib.cli import dispatch


class FakeManager:
    def __init__(self) -> None:
        self.call: tuple | None = None

    def logs(self, pack_id: str, *, lines: int, follow: bool) -> None:
        self.call = ("logs", pack_id, lines, follow)

    def console(self, pack_id: str, command: str) -> None:
        self.call = ("console", pack_id, command)


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


if __name__ == "__main__":
    unittest.main()
