import importlib
import os
import tempfile
import unittest
from pathlib import Path


class ConfigTest(unittest.TestCase):
    def reload_config(self):
        import log_rotator.config as config
        return importlib.reload(config)

    def tearDown(self):
        for var in ("LOG_ROTATOR_LOG_DIR", "LOG_ROTATOR_ARCHIVE_DIR", "LOG_ROTATOR_EXTRA_ROOTS"):
            os.environ.pop(var, None)
        self.reload_config()

    def test_defaults_point_inside_project(self):
        config = self.reload_config()
        self.assertEqual(config.LOG_DIR, config.PROJECT_ROOT / "logs")
        self.assertEqual(config.ARCHIVE_DIR, config.PROJECT_ROOT / "rotated_logs")
        self.assertEqual(config.ALLOWED_LOG_ROOTS, [config.LOG_DIR])
        self.assertTrue(config.LOG_DIR.is_absolute())

    def test_environment_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["LOG_ROTATOR_LOG_DIR"] = tmp
            os.environ["LOG_ROTATOR_ARCHIVE_DIR"] = os.path.join(tmp, "archives")
            os.environ["LOG_ROTATOR_EXTRA_ROOTS"] = "/srv/app-logs"
            config = self.reload_config()
            self.assertEqual(config.LOG_DIR, Path(tmp).resolve())
            self.assertEqual(config.ARCHIVE_DIR, Path(tmp).resolve() / "archives")
            self.assertEqual(config.ALLOWED_LOG_ROOTS,
                             [Path(tmp).resolve(), Path("/srv/app-logs")])

    def test_default_log_has_alias(self):
        config = self.reload_config()
        self.assertEqual(config.LOG_ALIASES["apache error"], config.DEFAULT_LOG_NAME)


if __name__ == "__main__":
    unittest.main()
