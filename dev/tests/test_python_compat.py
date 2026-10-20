from __future__ import annotations

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class PythonCompatibilityTests(unittest.TestCase):
    def test_runtime_sources_parse_with_python_39_grammar(self) -> None:
        for directory in (ROOT / "terminal", ROOT / "lib", ROOT / "manager/src", ROOT / "monitor/src"):
            for source in directory.rglob("*.py"):
                with self.subTest(source=source):
                    ast.parse(
                        source.read_text(encoding="utf-8"),
                        filename=str(source), feature_version=(3, 9),
                    )

    def test_annotated_modules_postpone_annotation_evaluation(self) -> None:
        for directory in (ROOT / "terminal", ROOT / "lib", ROOT / "manager/src", ROOT / "monitor/src"):
            for source in directory.rglob("*.py"):
                text = source.read_text(encoding="utf-8")
                if any(token in text for token in (" | ", "list[", "dict[", "tuple[")):
                    with self.subTest(source=source):
                        self.assertIn("from __future__ import annotations", text)


if __name__ == "__main__":
    unittest.main()
