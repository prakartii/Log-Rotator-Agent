"""Tests for flock-based locking (log_rotator/tools/locking.py)."""

import fcntl
import os
import tempfile
import unittest
from pathlib import Path

from log_rotator.tools import locking


class LockPathTest(unittest.TestCase):
    def test_lock_file_is_hidden_next_to_the_log(self):
        path = locking.lock_path_for("/srv/logs/apache_error.log")
        self.assertEqual(path, Path("/srv/logs/.apache_error.log.rotate.lock"))


class AcquireTest(unittest.TestCase):
    """flock locks belong to the OPEN FILE DESCRIPTION: two separate open() calls
    on the same file conflict even inside one process, which lets us test
    lock contention without starting another process."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "file.lock"
        self.path.touch()
        self.fds = []

    def tearDown(self):
        for fd in self.fds:
            os.close(fd)
        self.tmp.cleanup()

    def open(self):
        fd = os.open(self.path, os.O_RDWR)
        self.fds.append(fd)
        return fd

    def test_free_lock_is_taken_without_waiting(self):
        waited = locking.acquire(self.open(), fcntl.LOCK_EX, timeout=0)
        self.assertIsNotNone(waited)
        self.assertLess(waited, 50)

    def test_busy_exclusive_lock_returns_none_without_timeout(self):
        fcntl.flock(self.open(), fcntl.LOCK_EX)
        self.assertIsNone(locking.acquire(self.open(), fcntl.LOCK_EX, timeout=0))

    def test_waits_until_timeout_before_giving_up(self):
        import time
        fcntl.flock(self.open(), fcntl.LOCK_EX)
        start = time.monotonic()
        self.assertIsNone(locking.acquire(self.open(), fcntl.LOCK_EX, timeout=0.2))
        self.assertGreaterEqual(time.monotonic() - start, 0.2)


if __name__ == "__main__":
    unittest.main()
