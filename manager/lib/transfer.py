"""由 Manager 定义的安全整合包传输格式。"""

from __future__ import annotations

import shutil
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

from .config import PACKS_DIR, load_pack, validate_pack_id
from .modpacks import ModpackError, ModpackManager


def export_pack(pack_id: str, packs_dir: Path = PACKS_DIR) -> Path:
    validate_pack_id(pack_id)
    source = packs_dir / pack_id
    if not (source / "src").is_dir() or not (source / "config.yml").is_file():
        raise ModpackError(f"整合包不存在或不完整：{pack_id}")
    if any(path.is_symlink() for path in source.joinpath("src").rglob("*")):
        raise ModpackError("整合包 src/ 不能包含符号链接")
    directory = Path(tempfile.mkdtemp(prefix="mcsp-pack-"))
    output = directory / f"{pack_id}.tar.gz"
    with tarfile.open(output, "w:gz") as archive:
        archive.add(source / "config.yml", arcname="config.yml")
        archive.add(source / "src", arcname="src")
    return output


def import_pack(
    manager: ModpackManager, pack_id: str, archive_path: Path, *, force: bool,
) -> Path:
    validate_pack_id(pack_id)
    target = manager.packs_dir / pack_id
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        raise ModpackError(f"整合包目标不是安全目录：{pack_id}")
    if target.exists() and not force:
        raise ModpackError(f"整合包已存在：{pack_id}；确认覆盖请使用 --force")
    for name in ("src", "config.yml", "data", "backups"):
        if (target / name).is_symlink():
            raise ModpackError(f"整合包目标包含符号链接：{pack_id}/{name}")
    temporary = Path(tempfile.mkdtemp(prefix="mcsp-pack-import-"))
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            members = archive.getmembers()
            names: set[str] = set()
            for member in members:
                path = PurePosixPath(member.name)
                allowed = path == PurePosixPath("config.yml") or (
                    path.parts and path.parts[0] == "src"
                )
                if (
                    not allowed or path.is_absolute() or ".." in path.parts
                    or member.issym() or member.islnk()
                    or not (member.isfile() or member.isdir())
                ):
                    raise ModpackError(f"整合包归档包含不安全路径：{member.name}")
                normalized = path.as_posix().rstrip("/")
                if normalized in names:
                    raise ModpackError(f"整合包归档包含重复路径：{member.name}")
                names.add(normalized)
            archive.extractall(temporary)
        if not (temporary / "src").is_dir() or not (temporary / "config.yml").is_file():
            raise ModpackError("整合包归档缺少 src/ 或 config.yml")
        target.mkdir(parents=True, exist_ok=True)
        source_target = target / "src"
        if source_target.exists():
            shutil.rmtree(source_target)
        shutil.move(str(temporary / "src"), source_target)
        shutil.copy2(temporary / "config.yml", target / "config.yml")
        for name in ("data", "backups"):
            (target / name).mkdir(exist_ok=True)
        manager.configure(pack_id)
        return target
    except (OSError, tarfile.TarError) as exc:
        raise ModpackError(f"整合包归档处理失败：{exc}") from exc
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def import_directory(
    manager: ModpackManager, pack_id: str, source: Path,
) -> Path:
    """用受管暂存区更新 src/config，同时保留运行数据与备份。"""
    validate_pack_id(pack_id)
    try:
        source = source.resolve(strict=True)
        transfer_root = (manager.manager_root / "tmp/transfers").resolve()
        source.relative_to(transfer_root)
    except (OSError, ValueError) as exc:
        raise ModpackError("整合包暂存目录不在 Manager 受管范围内") from exc
    if source != transfer_root / pack_id:
        raise ModpackError("整合包暂存目录与整合包 ID 不匹配")
    if not (source / "src").is_dir() or not (source / "config.yml").is_file():
        raise ModpackError("整合包暂存目录缺少 src/ 或 config.yml")
    if any(path.is_symlink() for path in source.rglob("*")):
        raise ModpackError("整合包暂存目录不能包含符号链接")
    load_pack(pack_id, transfer_root)

    target = manager.packs_dir / pack_id
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        raise ModpackError(f"整合包目标不是安全目录：{pack_id}")
    for name in ("src", "config.yml", "data", "backups"):
        if (target / name).is_symlink():
            raise ModpackError(f"整合包目标包含符号链接：{pack_id}/{name}")

    temporary = manager.manager_root / "tmp" / f"import-{pack_id}"
    shutil.rmtree(temporary, ignore_errors=True)
    target_existed = target.exists()
    old_source = temporary / "old-src"
    old_config = temporary / "old-config.yml"
    try:
        temporary.mkdir(parents=True)
        shutil.copytree(source / "src", temporary / "new-src")
        shutil.copy2(source / "config.yml", temporary / "new-config.yml")
        target.mkdir(parents=True, exist_ok=True)
        destination = target / "src"
        if destination.exists():
            destination.replace(old_source)
        config_target = target / "config.yml"
        if config_target.exists():
            config_target.replace(old_config)
        (temporary / "new-src").replace(destination)
        (temporary / "new-config.yml").replace(config_target)
        for name in ("data", "backups"):
            (target / name).mkdir(exist_ok=True)
        try:
            manager.configure(pack_id)
        except Exception:
            shutil.rmtree(destination, ignore_errors=True)
            config_target.unlink(missing_ok=True)
            if old_source.exists():
                old_source.replace(destination)
            if old_config.exists():
                old_config.replace(config_target)
            if not target_existed:
                shutil.rmtree(target, ignore_errors=True)
            raise
        return target
    except OSError as exc:
        raise ModpackError(f"整合包目录导入失败：{exc}") from exc
    finally:
        shutil.rmtree(temporary, ignore_errors=True)
