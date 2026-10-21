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
    def test_component_staging_excludes_runtime_secrets(self):
        with tempfile.TemporaryDirectory() as raw:
            target = Path(raw) / "terminal"
            release.stage_component("terminal", target)
            self.assertFalse((target / ".env").exists())
            self.assertFalse((target / "config.yml").exists())
            self.assertTrue((target / ".env.example").is_file())
            self.assertTrue((target / "config.yml.example").is_file())

    def test_archive_paths_are_confined(self):
        release._check_archive_names(["MC-ServerPack/file", "payloads/manager/"])
        with self.assertRaises(release.ReleaseError):
            release._check_archive_names(["../secret"])
        with self.assertRaises(release.ReleaseError):
            release._check_archive_names(["/etc/passwd"])

    def test_shared_sources_are_synchronized(self):
        release.check_shared()


if __name__ == "__main__":
    unittest.main()
