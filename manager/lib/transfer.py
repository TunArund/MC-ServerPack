"""由 Manager 定义的安全整合包传输格式。"""

from __future__ import annotations

import shutil
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

from .config import PACKS_DIR, validate_pack_id
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
