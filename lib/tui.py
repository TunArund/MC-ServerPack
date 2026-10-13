"""Small interactive frontend; it dispatches MC-ServerPack commands only."""

from __future__ import annotations

import shlex

MODULES = ("tui", "deploy", "modpack", "manager", "gateway", "bot", "cos")
ENTERABLE = set(MODULES) - {"tui"}


def split_command(line: str) -> list[str]:
    lexer = shlex.shlex(line, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    lexer.escape = ""  # Preserve backslashes in Windows paths.
    return list(lexer)


def run(dispatch) -> int:
    print("MC-ServerPack 交互模式；输入 help 查看命令，back 返回，exit 退出。")
    module: str | None = None
    while True:
        try:
            prompt = f"mc {module}> " if module else "mc> "
            line = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            continue
        if line in ("exit", "quit"):
            return 0
        if line == "back":
            module = None
            continue
        if line == "help":
            arguments = [module, "--help"] if module else ["--help"]
        else:
            arguments = split_command(line)
            if module is None and len(arguments) == 1 and arguments[0] in ENTERABLE:
                module = arguments[0]
                continue
            if module is not None:
                if len(arguments) == 1 and arguments[0] in ENTERABLE:
                    module = arguments[0]
                    continue
                arguments.insert(0, module)
        try:
            dispatch(arguments, interactive=True)
        except SystemExit:
            pass
        except Exception as exc:
            print(f"错误：{exc}")
