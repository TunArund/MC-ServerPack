#!/usr/bin/env python3
"""Safely extract only world/ from a manager backup."""

from __future__ import annotations

import argparse
import sys
import tarfile
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    args.destination.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(args.archive, "r:gz") as archive:
            members = []
            for member in archive.getmembers():
                path = Path(member.name)
                if path.is_absolute() or ".." in path.parts:
                    raise RuntimeError(f"备份包含不安全路径：{member.name}")
                if not (member.isdir() or member.isfile()):
                    raise RuntimeError(f"备份包含不支持的文件类型：{member.name}")
                # Compatible with current world/ archives and former data/world/ archives.
                if path.parts[:2] == ("data", "world"):
                    member.name = Path(*path.parts[1:]).as_posix()
                    members.append(member)
                elif path.parts[:1] == ("world",):
                    members.append(member)
            if not members:
                raise RuntimeError("备份中没有 world/ 存档")
            archive.extractall(args.destination, members=members, filter="data")
    except (OSError, tarfile.TarError) as exc:
        raise RuntimeError(f"无法读取备份：{exc}") from exc
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"错误：{exc}", file=sys.stderr); raise SystemExit(2)
