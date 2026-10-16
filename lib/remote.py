# 远程连接 命令执行
"""Safe subprocess, SSH, SCP and source-archive helpers for Terminal."""

from __future__ import annotations

import shlex
import shutil
import subprocess
import tarfile
import tempfile
from collections.abc import Iterable, Sequence
from pathlib import Path


class CommandError(RuntimeError):
    pass


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


def shell_join(arguments: Iterable[str]) -> str:
    return " ".join(shlex.quote(str(item)) for item in arguments)


def target(user: str, host: str) -> str:
    if not user or not host or any(char.isspace() for char in user + host):
        raise CommandError("SSH 用户或主机无效")
    return f"{user}@{host}"


def ssh(
    destination: str, arguments: Sequence[str], *, input_text: str | None = None,
    capture: bool = False, tty: bool = False, check: bool = True,
) -> subprocess.CompletedProcess:
    options = ["-t", "-o", "BatchMode=yes"] if tty else ["-o", "BatchMode=yes"]
    data = None if input_text is None else input_text.replace("\r\n", "\n").encode()
    return run(
        ["ssh", *options, destination, shell_join(arguments)],
        input_data=data, capture=capture, check=check,
    )


def scp(source: Path, destination: str) -> None:
    run(["scp", "-q", str(source), destination])


def archive_tree(source: Path, *, exclude: set[str] | None = None) -> Path:
    if not source.is_dir():
        raise CommandError(f"归档源目录不存在：{source}")
    temporary = Path(tempfile.mkdtemp(prefix="mcsp-upload-"))
    output = temporary / "source.tar.gz"
    excluded = {Path(item) for item in (exclude or set())}
    with tarfile.open(output, "w:gz") as archive:
        for path in sorted(source.rglob("*")):
            relative = path.relative_to(source)
            if "__pycache__" in relative.parts or path.suffix == ".pyc":
                continue
            if any(relative == item or item in relative.parents for item in excluded):
                continue
            archive.add(path, arcname=relative.as_posix(), recursive=False)
    return output


def cleanup_archive(path: Path) -> None:
    shutil.rmtree(path.parent, ignore_errors=True)


class Transport:
    """Injectable SSH/SCP boundary used by deployment and modpack services."""

    def ssh(self, destination: str, arguments: list[str], **kwargs):
        return ssh(destination, arguments, **kwargs)

    def scp(self, source: Path, destination: str) -> None:
        scp(source, destination)

    def archive(self, source: Path, exclude: set[str] | None = None) -> Path:
        return archive_tree(source, exclude=exclude)

    def cleanup(self, archive: Path) -> None:
        cleanup_archive(archive)
