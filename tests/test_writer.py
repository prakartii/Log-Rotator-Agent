"""Tests for writer.py, the simulated process that appends to a log.

These tests run the writer as a real, separate process (subprocess), because
the whole point of the project is how the kernel treats a file that another
process still has open.
"""

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WRITER = PROJECT_ROOT / "writer.py"


def wait_until(condition, timeout=5.0, interval=0.02):
    """Poll `condition` until it is true or the timeout expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(interval)
    return False


class WriterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = os.path.join(self.tmp.name, "apache_error.log")
        self.procs = []

    def tearDown(self):
        for proc in self.procs:
            if proc.poll() is None:
                proc.terminate()
            proc.wait(timeout=5)
            proc.stdout.close()
        self.tmp.cleanup()

    def start_writer(self, *extra):
        proc = subprocess.Popen(
            [sys.executable, str(WRITER), self.log, *extra],
            cwd=PROJECT_ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        self.procs.append(proc)
        return proc

    def size(self):
        return os.stat(self.log).st_size

    def test_writes_requested_number_of_lines(self):
        proc = self.start_writer("--count", "25", "--rate", "0", "--quiet")
        self.assertEqual(proc.wait(timeout=10), 0)
        lines = Path(self.log).read_text().splitlines()
        self.assertEqual(len(lines), 25)
        self.assertEqual([int(l.rsplit("seq=", 1)[1]) for l in lines], list(range(1, 26)))
        self.assertTrue(all(l.startswith("[") for l in lines))

    def test_prefill_creates_large_log(self):
        proc = self.start_writer("--prefill-mb", "1", "--count", "0", "--quiet")
        self.assertEqual(proc.wait(timeout=10), 0)
        self.assertGreaterEqual(self.size(), 1024 * 1024)

    def test_append_writer_survives_truncation_on_same_inode(self):
        """Core idea of the project: truncate in place, the writer keeps going."""
        proc = self.start_writer("--rate", "200", "--quiet")
        self.assertTrue(wait_until(lambda: os.path.exists(self.log) and self.size() > 2000))
        inode_before = os.stat(self.log).st_ino

        os.truncate(self.log, 0)  # what the rotator will do (via ftruncate)

        self.assertTrue(wait_until(lambda: self.size() > 0), "writer did not continue")
        self.assertIsNone(proc.poll(), "writer process died after truncation")
        self.assertEqual(os.stat(self.log).st_ino, inode_before)

        # With O_APPEND the next write lands at offset 0: no hole, no zero bytes.
        data = Path(self.log).read_bytes()
        self.assertNotIn(b"\0", data)
        self.assertTrue(data.startswith(b"["))

    def test_non_append_writer_creates_sparse_hole_after_truncation(self):
        """Without O_APPEND the writer keeps its old offset -> zero-filled gap."""
        proc = self.start_writer("--rate", "200", "--no-append", "--quiet")
        self.assertTrue(wait_until(lambda: os.path.exists(self.log) and self.size() > 2000))
        offset_estimate = self.size()

        os.truncate(self.log, 0)

        self.assertTrue(wait_until(lambda: self.size() > offset_estimate))
        self.assertIsNone(proc.poll())
        data = Path(self.log).read_bytes()
        self.assertTrue(data.startswith(b"\0" * 1000),
                        "expected the start of the file to be a hole of zero bytes")

    def test_writer_reports_when_its_log_is_deleted(self):
        """Deleting the active log is the WRONG way to rotate; the writer notices nlink=0."""
        proc = self.start_writer("--rate", "100", "--status-interval", "0.05")
        self.assertTrue(wait_until(lambda: os.path.exists(self.log) and self.size() > 0))

        os.unlink(self.log)
        time.sleep(0.3)
        self.assertIsNone(proc.poll(), "writer keeps running, but on an unnamed inode")
        self.assertFalse(os.path.exists(self.log), "deleted log is not recreated by the writer")

        proc.terminate()
        output, _ = proc.communicate(timeout=5)
        self.assertIn("nlink=0", output)
        self.assertIn("DELETED", output)

    def test_cooperative_writer_writes_all_lines(self):
        proc = self.start_writer("--count", "25", "--rate", "0", "--quiet", "--cooperative")
        self.assertEqual(proc.wait(timeout=10), 0)
        self.assertEqual(len(Path(self.log).read_text().splitlines()), 25)

    def test_cooperative_writer_pauses_while_rotator_holds_lock(self):
        import fcntl
        proc = self.start_writer("--rate", "200", "--quiet", "--cooperative")
        self.assertTrue(wait_until(lambda: os.path.exists(self.log) and self.size() > 1000))

        rotator = os.open(self.log, os.O_RDWR)
        try:
            fcntl.flock(rotator, fcntl.LOCK_EX)
            time.sleep(0.05)  # let a write that was already in progress finish
            paused_at = self.size()
            time.sleep(0.3)
            self.assertEqual(self.size(), paused_at, "writer must wait for the exclusive lock")
            self.assertIsNone(proc.poll())
            fcntl.flock(rotator, fcntl.LOCK_UN)
            self.assertTrue(wait_until(lambda: self.size() > paused_at), "writer did not resume")
        finally:
            os.close(rotator)

    def test_normal_writer_ignores_the_lock(self):
        """flock is advisory: a writer that never calls flock() is not stopped."""
        import fcntl
        proc = self.start_writer("--rate", "200", "--quiet")
        self.assertTrue(wait_until(lambda: os.path.exists(self.log) and self.size() > 1000))
        rotator = os.open(self.log, os.O_RDWR)
        try:
            fcntl.flock(rotator, fcntl.LOCK_EX)
            locked_at = self.size()
            self.assertTrue(wait_until(lambda: self.size() > locked_at, timeout=2))
            self.assertIsNone(proc.poll())
        finally:
            os.close(rotator)

    def test_stops_cleanly_on_sigterm(self):
        proc = self.start_writer("--rate", "50")
        self.assertTrue(wait_until(lambda: os.path.exists(self.log) and self.size() > 0))
        proc.terminate()
        output, _ = proc.communicate(timeout=5)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("stopping after", output)


if __name__ == "__main__":
    unittest.main()
