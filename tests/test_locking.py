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

    def test_lock_released_while_waiting_is_acquired(self):
        import threading
        holder = self.open()
        fcntl.flock(holder, fcntl.LOCK_EX)
        threading.Timer(0.1, fcntl.flock, (holder, fcntl.LOCK_UN)).start()
        waited = locking.acquire(self.open(), fcntl.LOCK_EX, timeout=2)
        self.assertIsNotNone(waited)
        self.assertGreaterEqual(waited, 90)

    def test_shared_locks_do_not_block_each_other(self):
        fcntl.flock(self.open(), fcntl.LOCK_SH)
        self.assertIsNotNone(locking.acquire(self.open(), fcntl.LOCK_SH, timeout=0))

    def test_shared_lock_blocks_exclusive(self):
        fcntl.flock(self.open(), fcntl.LOCK_SH)
        self.assertIsNone(locking.acquire(self.open(), fcntl.LOCK_EX, timeout=0))


class RotationLockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Path(self.tmp.name) / "apache_error.log"
        self.log.write_bytes(b"line\n")
        self.lock_file = locking.lock_path_for(self.log)

    def tearDown(self):
        self.tmp.cleanup()

    def test_creates_private_lock_file(self):
        with locking.rotation_lock(self.log) as info:
            self.assertEqual(info["lock_file"], str(self.lock_file))
            self.assertTrue(self.lock_file.exists())
            self.assertEqual(os.stat(self.lock_file).st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
