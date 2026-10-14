#!/usr/bin/env python3
"""Find cache-like paths in a modpack src directory."""

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACK_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def main() -> int:
    if len(sys.argv) != 2 or not PACK_RE.fullmatch(sys.argv[1]):
        print("用法：manager.sh config --scan <整合包ID>", file=sys.stderr)
        return 2
    source = ROOT / "modpacks" / sys.argv[1] / "src"
    if not source.is_dir():
        print(f"错误：找不到 {source}", file=sys.stderr)
        return 2

    matches: list[str] = []
    for current, directories, files in os.walk(source, followlinks=False):
        base = Path(current)
        matched = [name for name in directories if "cache" in name.casefold()]
        for name in sorted(matched):
            matches.append((base / name).relative_to(source).as_posix() + "/")
        directories[:] = [name for name in directories if name not in matched]
        for name in sorted(files):
            if "cache" in name.casefold():
                matches.append((base / name).relative_to(source).as_posix())

    if not matches:
        print("未发现名称包含 cache 的文件或目录。")
        return 0
    print("# 可按需加入 config.yml 的 read-only.persistent：")
    for path in sorted(matches):
        print(f"  - {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
