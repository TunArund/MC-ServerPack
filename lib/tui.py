"""Small interactive frontend; it dispatches MC-ServerPack commands only."""

from __future__ import annotations

import shlex


def split_command(line: str) -> list[str]:
    lexer = shlex.shlex(line, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    lexer.escape = ""  # Preserve backslashes in Windows paths.
    return list(lexer)


def run(dispatch) -> int:
    print("MC-ServerPack 交互模式；输入 help 查看命令，exit 退出。")
    while True:
        try:
            line = input("mc> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            continue
        if line in ("exit", "quit"):
            return 0
        if line == "help":
            line = "--help"
        try:
            dispatch(split_command(line), interactive=True)
        except SystemExit:
            pass
        except Exception as exc:
            print(f"错误：{exc}")
