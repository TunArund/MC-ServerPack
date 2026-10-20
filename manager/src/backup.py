"""以原子方式备份和恢复整合包持久化数据。"""

from __future__ import annotations

import hashlib
import os
import shutil
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path, PurePosixPath

from .config import PackSpec


class BackupError(RuntimeError):
    pass


def backup_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def backup_id(path: Path) -> str:
    """以归档内容生成稳定短 ID，重命名不会改变备份身份。"""
    return backup_sha256(path)[:12]


def _reject_links(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_symlink():
            raise BackupError(f"持久化数据中不允许符号链接：{path.relative_to(root)}")


def create_backup(spec: PackSpec, now: datetime | None = None, *, prune: bool = True) -> Path:
    data = spec.directory / "data"
    if not data.is_dir():
        raise BackupError(f"{spec.pack_id} 缺少 data/ 目录")
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
        raise BackupError(f"备份失败：{exc}") from exc
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
            raise BackupError("只能指定 backups/ 中的文件名或备份 ID")
        source = spec.directory / "backups" / name
        if not source.is_file():
            if len(name) < 4 or any(character not in "0123456789abcdef" for character in name):
                raise BackupError(f"找不到备份：{name}")
            matches = [path for path in list_backups(spec) if backup_id(path).startswith(name)]
            if len(matches) > 1:
                raise BackupError(f"备份 ID 前缀不唯一，请补充更多字符：{name}")
            if matches:
                source = matches[0]
    else:
        candidates = list_backups(spec)
        if not candidates:
            raise BackupError("没有可恢复的备份")
        source = candidates[0]
    if not source.is_file():
        raise BackupError(f"找不到备份：{source.name}")
    return source


def _validate_members(archive: tarfile.TarFile) -> list[tarfile.TarInfo]:
    members = archive.getmembers()
    for member in members:
        path = PurePosixPath(member.name)
        if path.is_absolute() or not path.parts or path.parts[0] != "data" or ".." in path.parts:
            raise BackupError(f"备份包含不安全路径：{member.name}")
        if not (member.isfile() or member.isdir()):
            raise BackupError(f"备份包含链接或特殊文件：{member.name}")
    return members


def restore_backup(spec: PackSpec, name: str | None = None, *, keep_previous: bool = False) -> Path:
    source = resolve_backup(spec, name)
    parent = spec.directory
    with tempfile.TemporaryDirectory(prefix=".restore-", dir=parent) as raw:
        temporary = Path(raw)
        try:
            with tarfile.open(source, "r:gz") as archive:
                members = _validate_members(archive)
                archive.extractall(temporary, members=members)
        except (OSError, tarfile.TarError) as exc:
            raise BackupError(f"备份无法读取：{exc}") from exc
        restored = temporary / "data"
        if not restored.is_dir():
            raise BackupError("备份缺少 data/ 目录")
        current = parent / "data"
        previous = parent / ".data-before-restore"
        if previous.exists():
            raise BackupError(f"发现未处理的恢复目录：{previous}")
        try:
            if current.exists():
                current.rename(previous)
            restored.rename(current)
        except OSError as exc:
            if not current.exists() and previous.exists():
                previous.rename(current)
            raise BackupError(f"替换数据失败：{exc}") from exc
        if not keep_previous:
            shutil.rmtree(previous, ignore_errors=True)
    return source


def finish_restore(spec: PackSpec) -> None:
    shutil.rmtree(spec.directory / ".data-before-restore", ignore_errors=True)


def rollback_restore(spec: PackSpec) -> None:
    current = spec.directory / "data"
    previous = spec.directory / ".data-before-restore"
    if not previous.is_dir():
        raise BackupError("恢复回滚点不存在")
    failed = spec.directory / ".data-failed-restore"
    shutil.rmtree(failed, ignore_errors=True)
    if current.exists(): current.rename(failed)
    previous.rename(current)
    shutil.rmtree(failed, ignore_errors=True)
