"""Small atomic state file for completed daily power events."""

from __future__ import annotations

import json
import os
from datetime import date, timedelta
from pathlib import Path


class StateError(RuntimeError):
    pass


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.completed = self._load()

    def _load(self) -> set[str]:
        try:
            content = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return set()
        except OSError as exc:
            raise StateError(f"调度状态无法读取：{exc}") from exc
        try:
            value = json.loads(content)
        except ValueError as exc:
            raise StateError(f"调度状态格式无效：{exc}") from exc
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise StateError("调度状态必须是字符串数组")
        return set(value)

    def contains(self, key: str) -> bool:
        return key in self.completed

    def mark(self, key: str, today: date) -> None:
        cutoff = (today - timedelta(days=7)).isoformat()
        self.completed = {
            item for item in self.completed
            if len(item) >= 10 and item[:10] >= cutoff
        }
        self.completed.add(key)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            temporary.write_text(
                json.dumps(sorted(self.completed), ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self.path)
        except OSError as exc:
            raise StateError(f"调度状态无法写入：{exc}") from exc
