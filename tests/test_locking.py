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

    def test_second_rotation_is_refused_while_first_holds_lock(self):
        from log_rotator import errors
        from log_rotator.errors import RotatorError
        with locking.rotation_lock(self.log):
            with self.assertRaises(RotatorError) as ctx:
                with locking.rotation_lock(self.log, timeout=0):
                    self.fail("second rotator must not get the lock")
        self.assertEqual(ctx.exception.code, errors.ROTATION_IN_PROGRESS)
        self.assertEqual(ctx.exception.details["lock_file"], str(self.lock_file))

    def test_lock_is_released_after_the_block(self):
        with locking.rotation_lock(self.log):
            pass
        with locking.rotation_lock(self.log, timeout=0):
            pass

    def test_lock_is_released_when_the_block_raises(self):
        with self.assertRaises(ZeroDivisionError):
            with locking.rotation_lock(self.log):
                1 / 0
        with locking.rotation_lock(self.log, timeout=0):
            pass

    def test_lock_file_is_kept_after_release(self):
        """Deleting it would let a later rotator lock a different inode."""
        with locking.rotation_lock(self.log):
            inode = os.stat(self.lock_file).st_ino
        self.assertEqual(os.stat(self.lock_file).st_ino, inode)

    def test_symlink_at_lock_path_is_rejected(self):
        from log_rotator import errors
        from log_rotator.errors import RotatorError
        target = Path(self.tmp.name) / "elsewhere"
        target.write_bytes(b"")
        os.symlink(target, self.lock_file)
        with self.assertRaises(RotatorError) as ctx:
            with locking.rotation_lock(self.log):
                pass
        self.assertEqual(ctx.exception.code, errors.SYMLINK_REJECTED)


PROJECT_ROOT = Path(__file__).resolve().parent.parent

HOLD_LOCK = """
import sys, time
from log_rotator.tools import locking
with locking.rotation_lock(sys.argv[1]):
    print("locked", flush=True)
    time.sleep(30)
"""


class RotationLockAcrossProcessesTest(unittest.TestCase):
    """The real situation: another rotator PROCESS holds the lock."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Path(self.tmp.name) / "apache_error.log"
        self.log.write_bytes(b"line\n")
        import subprocess
        import sys
        self.holder = subprocess.Popen([sys.executable, "-c", HOLD_LOCK, str(self.log)],
                                       cwd=PROJECT_ROOT, stdout=subprocess.PIPE, text=True)
        self.assertEqual(self.holder.stdout.readline().strip(), "locked")

    def tearDown(self):
        if self.holder.poll() is None:
            self.holder.kill()
        self.holder.wait(timeout=5)
        self.holder.stdout.close()
        self.tmp.cleanup()

    def test_refusal_names_the_holding_process(self):
        from log_rotator.errors import RotatorError
        with self.assertRaises(RotatorError) as ctx:
            with locking.rotation_lock(self.log, timeout=0):
                pass
        self.assertEqual(ctx.exception.details["locked_by_pid"], self.holder.pid)

    def test_killed_rotator_releases_the_lock(self):
        """The kernel drops flocks when a process dies: no stale lock is left behind."""
        self.holder.kill()  # SIGKILL: no cleanup code in the holder runs
        self.holder.wait(timeout=5)
        with locking.rotation_lock(self.log, timeout=0):
            pass


if __name__ == "__main__":
    unittest.main()
