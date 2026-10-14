#!/usr/bin/env python3
"""Import a local file/archive or URL into a modpack src directory."""

from __future__ import annotations

import argparse
import shutil
import tarfile
import tempfile
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path


def safe_target(root: Path, name: str) -> Path:
    target = (root / name).resolve()
    if root.resolve() not in target.parents and target != root.resolve():
        raise RuntimeError(f"压缩包包含不安全路径：{name}")
    return target


def extract(source: Path, destination: Path) -> None:
    if zipfile.is_zipfile(source):
        with zipfile.ZipFile(source) as archive:
            for info in archive.infolist():
                target = safe_target(destination, info.filename)
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(info) as src, target.open("wb") as dst:
                        shutil.copyfileobj(src, dst)
        return
    if tarfile.is_tarfile(source):
        with tarfile.open(source, "r:*") as archive:
            for member in archive.getmembers():
                target = safe_target(destination, member.name)
                if not (member.isdir() or member.isfile()):
                    raise RuntimeError(f"压缩包包含链接或特殊文件：{member.name}")
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    extracted = archive.extractfile(member)
                    if extracted is None:
                        raise RuntimeError(f"无法读取压缩包成员：{member.name}")
                    with extracted, target.open("wb") as dst:
                        shutil.copyfileobj(extracted, dst)
        return
    shutil.copy2(source, destination / source.name)


def import_source(source_text: str, destination: Path, *, force: bool = False) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    existing = [path for path in destination.iterdir() if path.name != ".gitkeep"]
    if existing and not force:
        raise RuntimeError("src/ 非空；如需合并导入请追加 --force")

    parsed = urllib.parse.urlparse(source_text)
    if parsed.scheme in ("http", "https"):
        filename = Path(urllib.parse.unquote(parsed.path)).name or "download"
        with tempfile.TemporaryDirectory(prefix="mc-import-") as temp:
            downloaded = Path(temp) / filename
            print(f"下载：{source_text}")
            urllib.request.urlretrieve(source_text, downloaded)
            extract(downloaded, destination)
    else:
        source = Path(source_text).expanduser().resolve()
        if source.is_dir():
            resolved_destination = destination.resolve()
            if (
                source == resolved_destination
                or source in resolved_destination.parents
                or resolved_destination in source.parents
            ):
                raise RuntimeError("导入目录不能与 src/ 互相包含")
            for item in source.iterdir():
                target = destination / item.name
                if item.is_dir():
                    shutil.copytree(item, target, dirs_exist_ok=True)
                else:
                    shutil.copy2(item, target)
        elif source.is_file():
            extract(source, destination)
        else:
            raise RuntimeError(f"找不到导入文件：{source}")
    print(f"已导入到：{destination}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source")
    parser.add_argument("destination", type=Path)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    import_source(args.source, args.destination, force=args.force)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, tarfile.TarError, zipfile.BadZipFile) as exc:
        print(f"错误：{exc}")
        raise SystemExit(2)
