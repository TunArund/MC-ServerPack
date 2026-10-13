"""PyInstaller entry point for the optional Windows mc.exe frontend."""

from __future__ import annotations

import sys
from pathlib import Path

if __package__:
    from .cli import main
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from lib.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
