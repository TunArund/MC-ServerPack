from __future__ import annotations

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RuntimeBoundaryTests(unittest.TestCase):
    def test_power_core_is_identical_in_all_runtimes(self) -> None:
        expected = (ROOT / "lib/power.py").read_bytes()
        self.assertEqual(expected, (ROOT / "manager/lib/power.py").read_bytes())
        self.assertEqual(expected, (ROOT / "monitor/src/monitor/power.py").read_bytes())

    def test_shared_provider_implementations_are_identical(self) -> None:
        for name in ("tencent_api.py", "example_api.py"):
            expected = (ROOT / "lib/providers" / name).read_bytes()
            with self.subTest(name=name, runtime="manager"):
                self.assertEqual(expected, (ROOT / "manager/lib/providers" / name).read_bytes())
            with self.subTest(name=name, runtime="monitor"):
                self.assertEqual(expected, (ROOT / "monitor/src/monitor/providers" / name).read_bytes())

    def test_no_cross_runtime_imports(self) -> None:
        forbidden = {
            "lib": ("manager", "monitor"),
            "manager/lib": ("monitor",),
            "monitor/src": ("manager",),
        }
        for directory, prefixes in forbidden.items():
            for path in (ROOT / directory).rglob("*.py"):
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

    def test_monitor_source_root_contains_only_global_modules(self) -> None:
        modules = sorted(path.name for path in (ROOT / "monitor/src").glob("*.py"))
        self.assertEqual(modules, ["__init__.py", "app.py", "config.py"])
        self.assertFalse((ROOT / "monitor/src/state.py").exists())

    def test_provider_implementations_do_not_import_power_state_machine(self) -> None:
        for directory in (
            ROOT / "lib/providers", ROOT / "manager/lib/providers",
            ROOT / "monitor/src/monitor/providers",
        ):
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
