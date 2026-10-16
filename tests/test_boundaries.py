from __future__ import annotations

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RuntimeBoundaryTests(unittest.TestCase):
    def test_power_core_is_identical_in_terminal_and_manager(self) -> None:
        self.assertEqual(
            (ROOT / "lib/power.py").read_bytes(),
            (ROOT / "manager/lib/power.py").read_bytes(),
        )

    def test_no_cross_runtime_imports(self) -> None:
        forbidden = {
            "lib": ("manager", "monitor"),
            "manager/lib": ("monitor",),
            "monitor/src": ("manager",),
        }
        for directory, prefixes in forbidden.items():
            for path in (ROOT / directory).glob("*.py"):
                with self.subTest(path=path):
                    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                    imports: list[str] = []
                    for node in ast.walk(tree):
                        if isinstance(node, ast.Import):
                            imports.extend(alias.name for alias in node.names)
                        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                            imports.append(node.module)
                    for name in imports:
                        self.assertFalse(name.startswith(prefixes), f"{path}: cross-runtime import {name}")


if __name__ == "__main__":
    unittest.main()
