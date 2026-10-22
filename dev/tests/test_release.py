import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("mcsp_release", ROOT / "dev/release/release.py")
release = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = release
SPEC.loader.exec_module(release)


class ReleaseTests(unittest.TestCase):
    def test_version_uses_two_numeric_parts_with_v_prefix(self):
        for value in ("v0.1", "v1.0", "v12.34"):
            self.assertEqual(release.current_version(value), value)
        for value in ("1.0", "v1", "v1.0.0", "v01.2", "v1.02", "v1.0-alpha"):
            with self.subTest(value=value), self.assertRaises(release.ReleaseError):
                release.current_version(value)

    def test_component_staging_excludes_runtime_secrets(self):
        with tempfile.TemporaryDirectory() as raw:
            target = Path(raw) / "terminal"
            release.stage_component("terminal", target)
            self.assertFalse((target / ".env").exists())
            self.assertFalse((target / "config.yml").exists())
            self.assertTrue((target / ".env.example").is_file())
            self.assertTrue((target / "config.yml.example").is_file())
            self.assertTrue((target / "LICENSE").is_file())
            self.assertTrue((target / "NOTICE").is_file())

    def test_archive_paths_are_confined(self):
        release._check_archive_names(["MC-ServerPack/file", "data/manager/"])
        with self.assertRaises(release.ReleaseError):
            release._check_archive_names(["../secret"])
        with self.assertRaises(release.ReleaseError):
            release._check_archive_names(["/etc/passwd"])

    def test_shared_sources_are_synchronized(self):
        release.check_shared()

    def test_data_tree_contains_all_replaceable_program_content(self):
        with tempfile.TemporaryDirectory() as raw:
            target = Path(raw) / "data"
            release.stage_data(release.Context("v0.0", True), target)
            self.assertTrue((target / "mcsp.pyz").is_file())
            self.assertTrue((target / "manager/manager.sh").is_file())
            self.assertTrue((target / "monitor/monitor.sh").is_file())
            self.assertTrue((target / "modpacks/template/config.yml").is_file())
            self.assertTrue((target / "LICENSE").is_file())
            self.assertTrue((target / "NOTICE").is_file())
            self.assertFalse((target / "config.yml").exists())
            self.assertFalse((target / ".env").exists())


if __name__ == "__main__":
    unittest.main()
