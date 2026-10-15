"""Atomic backup and restore of a modpack's persistent data directory."""

from __future__ import annotations

import os
import shutil
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path, PurePosixPath

from .config import PackSpec


class ArchiveError(RuntimeError):
    pass


def _reject_links(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ArchiveError(f"持久化数据中不允许符号链接：{path.relative_to(root)}")


def create_backup(spec: PackSpec, now: datetime | None = None, *, prune: bool = True) -> Path:
    data = spec.directory / "data"
    if not data.is_dir():
        raise ArchiveError(f"{spec.pack_id} 缺少 data/ 目录")
    _reject_links(data)
    backups = spec.directory / "backups"
    backups.mkdir(exist_ok=True)
    timestamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S-%f")
    target = backups / f"{spec.pack_id}-{timestamp}.tar.gz"
    temporary = backups / f".{target.name}.tmp"
    try:
        with tarfile.open(temporary, "w:gz") as archive:
            archive.add(data, arcname="data", recursive=True)
        os.replace(temporary, target)
    except (OSError, tarfile.TarError) as exc:
        temporary.unlink(missing_ok=True)
        raise ArchiveError(f"备份失败：{exc}") from exc
    if prune:
        prune_backups(spec, keep=target)
    return target


def list_backups(spec: PackSpec) -> list[Path]:
    backups = spec.directory / "backups"
    return sorted(backups.glob(f"{spec.pack_id}-*.tar.gz"), key=lambda item: item.stat().st_mtime, reverse=True)


def prune_backups(spec: PackSpec, *, keep: Path | None = None) -> None:
    backups = list_backups(spec)
    retained = 0
    for path in backups:
        if keep is not None and path == keep:
            retained += 1
        elif retained < spec.backup_retain:
            retained += 1
        else:
            path.unlink()


def resolve_backup(spec: PackSpec, name: str | None) -> Path:
    if name:
        if Path(name).name != name:
            raise ArchiveError("只能指定 backups/ 中的文件名")
        source = spec.directory / "backups" / name
    else:
        candidates = list_backups(spec)
        if not candidates:
            raise ArchiveError("没有可恢复的备份")
        source = candidates[0]
    if not source.is_file():
        raise ArchiveError(f"找不到备份：{source.name}")
    return source


def _validate_members(archive: tarfile.TarFile) -> list[tarfile.TarInfo]:
    members = archive.getmembers()
    for member in members:
        path = PurePosixPath(member.name)
        if path.is_absolute() or not path.parts or path.parts[0] != "data" or ".." in path.parts:
            raise ArchiveError(f"备份包含不安全路径：{member.name}")
        if not (member.isfile() or member.isdir()):
            raise ArchiveError(f"备份包含链接或特殊文件：{member.name}")
    return members


def restore_backup(spec: PackSpec, name: str | None = None) -> Path:
    source = resolve_backup(spec, name)
    parent = spec.directory
    with tempfile.TemporaryDirectory(prefix=".restore-", dir=parent) as raw:
        temporary = Path(raw)
        try:
            with tarfile.open(source, "r:gz") as archive:
                members = _validate_members(archive)
                archive.extractall(temporary, members=members)
        except (OSError, tarfile.TarError) as exc:
            raise ArchiveError(f"备份无法读取：{exc}") from exc
        restored = temporary / "data"
        if not restored.is_dir():
            raise ArchiveError("备份缺少 data/ 目录")
        current = parent / "data"
        previous = parent / ".data-before-restore"
        if previous.exists():
            raise ArchiveError(f"发现未处理的恢复目录：{previous}")
        try:
            if current.exists():
                current.rename(previous)
            restored.rename(current)
        except OSError as exc:
            if not current.exists() and previous.exists():
                previous.rename(current)
            raise ArchiveError(f"替换数据失败：{exc}") from exc
        shutil.rmtree(previous, ignore_errors=True)
    return source
