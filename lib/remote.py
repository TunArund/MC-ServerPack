"""Subprocess, SSH and archive helpers for the local controller."""

from __future__ import annotations

import shlex
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import Iterable, Sequence


class CommandError(RuntimeError):
    pass


def run(
    argv: Sequence[str], *, input_text: str | None = None, capture: bool = False,
    env: dict[str, str] | None = None, check: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        arguments = list(map(str, argv))
        if input_text is None:
            return subprocess.run(
                arguments, text=True, check=check, capture_output=capture, env=env,
            )

        # Text-mode pipes translate LF to CRLF on Windows. Shell scripts sent
        # through SSH must retain LF, otherwise Bash reads options such as
        # ``pipefail\r``. Supplying bytes bypasses the platform translation.
        result = subprocess.run(
            arguments, input=input_text.replace("\r\n", "\n").encode("utf-8"),
            text=False, check=False, capture_output=capture, env=env,
        )
        stdout = result.stdout.decode(errors="replace") if result.stdout is not None else None
        stderr = result.stderr.decode(errors="replace") if result.stderr is not None else None
        decoded = subprocess.CompletedProcess(arguments, result.returncode, stdout, stderr)
        if check and decoded.returncode:
            raise subprocess.CalledProcessError(
                decoded.returncode, arguments, output=stdout, stderr=stderr,
            )
        return decoded
    except FileNotFoundError as exc:
        raise CommandError(f"找不到命令：{argv[0]}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        suffix = f"：{detail}" if detail else ""
        raise CommandError(f"命令执行失败（{exc.returncode}）：{' '.join(argv)}{suffix}") from exc


def quote_command(argv: Iterable[str]) -> str:
    return " ".join(shlex.quote(str(arg)) for arg in argv)


def ssh(target: str, argv: Sequence[str], *, tty: bool = False, input_text: str | None = None,
        check: bool = True, capture: bool = False) -> subprocess.CompletedProcess[str]:
    flag = "-t" if tty else "-o"
    prefix = ["ssh", flag]
    if not tty:
        prefix.append("BatchMode=yes")
    return run([*prefix, target, quote_command(argv)], input_text=input_text, check=check, capture=capture)


def scp(source: Path, target: str) -> None:
    run(["scp", "-q", str(source), target])


def archive_tree(source: Path, *, exclude: set[str] | None = None) -> Path:
    """Create a temporary gzip tar and return its path; caller removes its parent."""
    temp = Path(tempfile.mkdtemp(prefix="mc-serverpack-"))
    output = temp / "upload.tar.gz"
    excluded = exclude or set()
    with tarfile.open(output, "w:gz") as archive:
        for path in sorted(source.rglob("*")):
            relative = path.relative_to(source)
            if any(relative == Path(item) or Path(item) in relative.parents for item in excluded):
                continue
            archive.add(path, arcname=relative.as_posix(), recursive=False)
    return output


def executable(name: str) -> bool:
    return shutil.which(name) is not None
