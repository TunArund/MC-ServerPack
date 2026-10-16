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

    def test_shared_provider_implementations_are_identical(self) -> None:
        for name in ("tencent_api.py", "example_api.py"):
            with self.subTest(name=name):
                self.assertEqual(
                    (ROOT / "lib/providers" / name).read_bytes(),
                    (ROOT / "manager/lib/providers" / name).read_bytes(),
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

    def test_provider_implementations_do_not_import_power_state_machine(self) -> None:
        for directory in (ROOT / "lib/providers", ROOT / "manager/lib/providers"):
            for path in directory.glob("*_api.py"):
                with self.subTest(path=path):
                    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                    for node in ast.walk(tree):
                        if isinstance(node, ast.ImportFrom) and node.module:
                            self.assertFalse(
                                node.module.endswith("power"),
                                f"{path}: provider must not import power.py",
                            )


if __name__ == "__main__":
    unittest.main()
