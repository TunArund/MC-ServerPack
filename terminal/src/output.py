"""Terminal 唯一的人类可读输出边界。"""

from __future__ import annotations

import sys
import json
from dataclasses import dataclass
from typing import Any


@dataclass
class Renderer:
    quiet: bool = False
    json_mode: bool = False
    _result: Any = None
    _error: str | None = None

    def configure(self, *, quiet: bool = False, json_mode: bool = False) -> None:
        self.quiet = quiet
        self.json_mode = json_mode
        self._result = None
        self._error = None

    def progress(self, message: str, *, end: str = "\n") -> None:
        if not self.quiet and (not self.json_mode or self.progress_is_tty):
            print(message, end=end, file=sys.stderr, flush=True)

    def result(self, value: str, *, end: str = "\n", data: bool = False) -> None:
        if self.json_mode:
            if data:
                try:
                    self._result = json.loads(value)
                except (TypeError, json.JSONDecodeError):
                    self._result = value.rstrip("\n")
            else:
                self._result = value.rstrip("\n")
            return
        if self.quiet and not data:
            return
        print(value, end=end, file=sys.stdout, flush=True)

    def error(self, message: str) -> None:
        if self.json_mode:
            self._error = message.removeprefix("错误：")
            return
        print(message, file=sys.stderr, flush=True)

    def ask(self, message: str) -> str:
        self.progress(message, end="")
        return input()

    def finish(self, exit_code: int) -> None:
        if not self.json_mode:
            return
        document = (
            {"ok": True, "result": self._result}
            if exit_code == 0 else
            {"ok": False, "error": {"message": self._error or "命令执行失败"}}
        )
        print(json.dumps(document, ensure_ascii=False, separators=(",", ":")))

    @property
    def progress_is_tty(self) -> bool:
        return not self.quiet and sys.stderr.isatty()


renderer = Renderer()
