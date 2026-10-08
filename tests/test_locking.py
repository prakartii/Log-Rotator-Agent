"""Tests for flock-based locking (log_rotator/tools/locking.py)."""

import os
import tempfile
import unittest
from pathlib import Path

from log_rotator.tools import locking


class LockPathTest(unittest.TestCase):
    def test_lock_file_is_hidden_next_to_the_log(self):
        path = locking.lock_path_for("/srv/logs/apache_error.log")
        self.assertEqual(path, Path("/srv/logs/.apache_error.log.rotate.lock"))


if __name__ == "__main__":
    unittest.main()
