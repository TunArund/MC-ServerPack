"""
远程连接与命令执行相关
安全的SSH、SCP

"""

from __future__ import annotations

import re
import shlex
import shutil
import subprocess
import sys
from collections import deque
from collections.abc import Iterable, Sequence
from datetime import datetime
from pathlib import Path


class CommandError(RuntimeError):
    pass


HOST_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?")
STEP_RE = re.compile(r"^\[[^\]]+ \d+/\d+\]\s+.+")


def run(
    arguments: Sequence[str], *, input_data: bytes | None = None,
    capture: bool = False, check: bool = True,
) -> subprocess.CompletedProcess:
    command = [str(item) for item in arguments]
    try:
        return subprocess.run(
            command, input=input_data, capture_output=capture, check=check,
        )
    except FileNotFoundError as exc:
        raise CommandError(f"找不到命令：{command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        raw = exc.stderr or exc.stdout or b""
        detail = raw.decode(errors="replace").strip() if isinstance(raw, bytes) else str(raw).strip()
        suffix = f"：{detail[-1000:]}" if detail else ""
        raise CommandError(f"命令执行失败（{exc.returncode}）：{command[0]}{suffix}") from exc


def visible_tail_lines() -> int:
    """部署尾窗约占终端高度三分之一，普通终端至少显示十行。"""
    rows = shutil.get_terminal_size(fallback=(80, 24)).lines
    return max(10, min(24, rows // 3))


def run_logged(
    arguments: Sequence[str], *, input_data: bytes | None, log_path: Path,
    label: str, check: bool = True,
) -> subprocess.CompletedProcess:
    """记录完整输出；TTY 中只绘制随视口缩放的实时尾部。"""
    command = [str(item) for item in arguments]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    tail: deque[str] = deque(maxlen=visible_tail_lines())
    interactive = sys.stdout.isatty()
    rendered = 0
    status = label
    width = max(20, shutil.get_terminal_size(fallback=(80, 24)).columns - 1)

    def redraw() -> None:
        nonlocal rendered
        lines = [status[:width], *(line[:width] for line in tail)]
        if rendered:
            sys.stdout.write(f"\x1b[{rendered}F")
        height = max(rendered, len(lines))
        for index in range(height):
            sys.stdout.write("\x1b[2K")
            if index < len(lines):
                sys.stdout.write(lines[index])
            sys.stdout.write("\n")
        sys.stdout.flush()
        rendered = height

    def clear() -> None:
        nonlocal rendered
        if not interactive or not rendered:
            return
        sys.stdout.write(f"\x1b[{rendered}F")
        for _ in range(rendered):
            sys.stdout.write("\x1b[2K\n")
        sys.stdout.write(f"\x1b[{rendered}F")
        sys.stdout.flush()
        rendered = 0

    if interactive:
        redraw()
    else:
        print(f"{label}（详细日志：{log_path}）", flush=True)
    try:
        process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8",
            errors="replace", bufsize=1,
        )
    except FileNotFoundError as exc:
        raise CommandError(f"找不到命令：{command[0]}") from exc

    assert process.stdin is not None
    assert process.stdout is not None
    try:
        if input_data is not None:
            process.stdin.write(input_data.decode("utf-8", "replace"))
        process.stdin.close()
    except BrokenPipeError:
        pass
    with log_path.open("a", encoding="utf-8") as log:
        log.write(
            f"\n[{datetime.now().astimezone().isoformat(timespec='seconds')}] "
            f"{label}\n$ {shell_join(command)}\n"
        )
        for raw in process.stdout:
            log.write(raw)
            log.flush()
            normalized = raw.replace("\r", "\n").splitlines()
            line = normalized[-1] if normalized else ""
            if line:
                if STEP_RE.fullmatch(line):
                    status = line
                else:
                    tail.append(line)
                if interactive:
                    redraw()
    process.stdout.close()
    returncode = process.wait()
    if returncode == 0:
        clear()
        summary = tail[-1] if tail else "完成"
        print(f"{label}：{summary}")
    else:
        if not interactive:
            if status != label:
                print(status)
            for line in tail:
                print(line)
        print(f"{label}失败；完整输出见 {log_path}", file=sys.stderr)
    result = subprocess.CompletedProcess(command, returncode)
    if check and returncode:
        raise CommandError(
            f"命令执行失败（{returncode}）：{command[0]}；详见 {log_path}"
        )
    return result


def shell_join(arguments: Iterable[str]) -> str:
    return " ".join(shlex.quote(str(item)) for item in arguments)


def target(user: str, host: str) -> str:
    if not user or not HOST_RE.fullmatch(host):
        raise CommandError("SSH 用户或主机无效")
    return f"{user}@{host}"


def ssh(
    destination: str, arguments: Sequence[str], *, input_text: str | None = None,
    capture: bool = False, tty: bool = False, check: bool = True,
    identity: Path | None = None,
) -> subprocess.CompletedProcess:
    options = ["-t", "-o", "BatchMode=yes"] if tty else ["-o", "BatchMode=yes"]
    if identity is not None:
        options.extend(("-i", str(identity)))
    data = None if input_text is None else input_text.replace("\r\n", "\n").encode()
    return run(
        ["ssh", *options, destination, shell_join(arguments)],
        input_data=data, capture=capture, check=check,
    )


def ssh_logged(
    destination: str, arguments: Sequence[str], *, input_text: str | None,
    log_path: Path, label: str, check: bool = True,
) -> subprocess.CompletedProcess:
    data = None if input_text is None else input_text.replace("\r\n", "\n").encode()
    return run_logged(
        ["ssh", "-o", "BatchMode=yes", destination, shell_join(arguments)],
        input_data=data, log_path=log_path, label=label, check=check,
    )


def scp(source: Path, destination: str) -> None:
    run(["scp", "-q", str(source), destination])


class Transport:
    """供部署与整合包服务注入的 SSH/SCP 边界。"""

    def __init__(self, log_path: Path | None = None) -> None:
        self.log_path = log_path

    def ssh(self, destination: str, arguments: list[str], **kwargs):
        return ssh(destination, arguments, **kwargs)

    def ssh_logged(
        self, destination: str, arguments: list[str], *, label: str,
        input_text: str | None = None, check: bool = True,
    ):
        if self.log_path is None:
            return self.ssh(
                destination, arguments, input_text=input_text, check=check,
            )
        return ssh_logged(
            destination, arguments, input_text=input_text,
            log_path=self.log_path, label=label, check=check,
        )

    def scp(self, source: Path, destination: str) -> None:
        scp(source, destination)
