"""Local modpack workspace operations."""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from .modpack_import import import_source

PACK_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def validate_id(pack: str) -> None:
    if not PACK_RE.fullmatch(pack):
        raise RuntimeError("整合包 ID 只能包含字母、数字、点、下划线和减号")


def create(manager: Path, pack: str) -> None:
    validate_id(pack)
    if pack == "template":
        raise RuntimeError("template 是保留名称")
    target = manager / "modpacks" / pack
    if target.exists():
        raise RuntimeError(f"整合包已存在：{pack}")
    target.mkdir(parents=True)
    shutil.copy2(manager / "modpacks/template/config.yml", target / "config.yml")
    for name in ("src", "data", "backups"):
        directory = target / name
        directory.mkdir()
        (directory / ".gitkeep").touch()
    print(f"已创建：{target}")


def scan(manager: Path, pack: str) -> None:
    validate_id(pack)
    source = manager / "modpacks" / pack / "src"
    if not source.is_dir():
        raise RuntimeError(f"找不到：{source}")
    matches: list[str] = []
    for current, directories, files in os.walk(source, followlinks=False):
        base = Path(current)
        cache_dirs = [name for name in directories if "cache" in name.casefold()]
        matches.extend((base / name).relative_to(source).as_posix() + "/" for name in cache_dirs)
        directories[:] = [name for name in directories if name not in cache_dirs]
        matches.extend((base / name).relative_to(source).as_posix() for name in files if "cache" in name.casefold())
    if not matches:
        print("未发现名称包含 cache 的文件或目录。")
        return
    print("# 可按需加入 config.yml 的 read-only.persistent：")
    for name in sorted(matches):
        print(f"  - {name}")


def import_into(manager: Path, pack: str, source: str, force: bool = False) -> None:
    validate_id(pack)
    destination = manager / "modpacks" / pack / "src"
    if not destination.is_dir():
        raise RuntimeError(f"请先创建整合包：{pack}")
    import_source(source, destination, force=force)
