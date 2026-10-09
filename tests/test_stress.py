"""Concurrency, writer and permission tests (phase 10).

These tests use real processes: several writer.py processes appending to the
same log, several agent.py processes rotating it at the same time, writers
without O_APPEND, and files or directories with restrictive permissions.
The main check is always the same: every line a writer wrote is found
exactly once, either in an archive or in the live log.
"""

import collections
import gzip
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from log_rotator import errors, rotate
from log_rotator.tools import locking

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WRITER = PROJECT_ROOT / "writer.py"
AGENT = PROJECT_ROOT / "agent.py"
LINE_ID = re.compile(rb"\[pid (\d+)\] .* seq=(\d+)\n")


@unittest.skipUnless(os.path.isdir("/proc"), "requires Linux")
class StressTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.logs = base / "logs"
        self.archives = base / "rotated_logs"
        self.logs.mkdir()
        self.log = self.logs / "apache_error.log"
        self.log.write_bytes(b"")
        self.writers = []

    def tearDown(self):
        self.stop_writers()
        self.tmp.cleanup()

    def start_writer(self, *options, rate=1000):
        proc = subprocess.Popen([sys.executable, str(WRITER), str(self.log), "--rate", str(rate),
                                 "--quiet", *options], stdout=subprocess.DEVNULL)
        self.writers.append(proc)
        return proc

    def stop_writers(self):
        for proc in self.writers:
            if proc.poll() is None:
                proc.terminate()
            proc.wait(timeout=5)

    def rotate(self, **kwargs):
        kwargs.setdefault("log_dir", self.logs)
        kwargs.setdefault("archive_dir", self.archives)
        return rotate.rotate_log("apache_error", **kwargs)

    def wait_for_size(self, size, timeout=5.0):
        deadline = time.monotonic() + timeout
        while os.stat(self.log).st_size < size and time.monotonic() < deadline:
            time.sleep(0.02)

    def line_counts(self):
        """How often each (writer pid, seq) line appears in all archives plus the live log."""
        data = self.log.read_bytes()
        for archive in sorted(self.archives.iterdir()) if self.archives.exists() else []:
            opener = gzip.open if archive.name.endswith(".gz") else open
            with opener(archive, "rb") as f:
                data += f.read()
        return collections.Counter(LINE_ID.findall(data))

    def assertEveryLineOnce(self, counts):
        duplicated = [line for line, n in counts.items() if n > 1]
        self.assertEqual(duplicated, [], "lines archived twice")
        by_writer = collections.defaultdict(set)
        for pid, seq in counts:
            by_writer[pid].add(int(seq))
        for pid, seqs in by_writer.items():
            self.assertEqual(seqs, set(range(1, max(seqs) + 1)), f"writer {pid} lost lines")


class MultipleWritersTest(StressTestCase):
    def test_three_cooperative_writers_lose_nothing(self):
        for _ in range(3):
            self.start_writer("--cooperative")
        results = []
        for _ in range(3):
            time.sleep(0.3)
            result = self.rotate()
            self.assertEqual(result["status"], "success", result)
            self.assertEqual(result["bytes_lost"], 0)
            results.append(result)
        time.sleep(0.2)
        self.stop_writers()
        counts = self.line_counts()
        self.assertEqual(len({pid for pid, _ in counts}), 3, "lines from all three writers")
        self.assertEveryLineOnce(counts)

    def test_plain_append_writers_never_duplicate_and_report_losses(self):
        """Writers without flock are not paused: a line can be lost, but only as reported."""
        for _ in range(3):
            self.start_writer()
        lost = 0
        for _ in range(3):
            time.sleep(0.3)
            result = self.rotate()
            self.assertEqual(result["status"], "success", result)
            lost += result["bytes_lost"]
        self.stop_writers()
        counts = self.line_counts()
        self.assertEqual([line for line, n in counts.items() if n > 1], [], "lines archived twice")
        if lost == 0:
            self.assertEveryLineOnce(counts)

    def test_every_writer_stays_attached_to_the_same_inode(self):
        writers = [self.start_writer(rate=200) for _ in range(3)]
        self.wait_for_size(4096)
        inode = os.stat(self.log).st_ino
        result = self.rotate()
        self.assertEqual(result["status"], "success", result)
        self.assertEqual(sorted(h["pid"] for h in result["open_by"]), sorted(w.pid for w in writers))
        self.assertTrue(result["rotation_checks"]["writers_attached"])
        time.sleep(0.2)
        for writer in writers:
            self.assertIsNone(writer.poll(), "writer must keep running")
        for handle in result["open_by"]:
            self.assertEqual(os.stat(f"/proc/{handle['pid']}/fd/{handle['fd']}").st_ino, inode)


class ConcurrentRotatorsTest(StressTestCase):
    def start_rotators(self, count, *options):
        command = [sys.executable, str(AGENT), "--json", "--log-dir", str(self.logs),
                   "--archive-dir", str(self.archives), "rotate", "apache_error", *options]
        return [subprocess.Popen(command, stdout=subprocess.PIPE, text=True) for _ in range(count)]

    def results(self, rotators):
        out = []
        for proc in rotators:
            stdout, _ = proc.communicate(timeout=60)
            out.append((proc.returncode, json.loads(stdout)))
        return out

    def test_four_waiting_rotators_all_succeed_one_after_another(self):
        self.start_writer("--cooperative")
        self.wait_for_size(8192)
        results = self.results(self.start_rotators(4, "--lock-timeout", "30"))
        for status, result in results:
            self.assertEqual((status, result["status"]), (0, "success"), result)
        self.assertEqual(len({r["archive"] for _, r in results}), 4, "four separate archives")
        time.sleep(0.2)
        self.stop_writers()
        self.assertEveryLineOnce(self.line_counts())

    def test_rotators_without_timeout_either_run_or_are_refused(self):
        self.start_writer("--cooperative")
        self.wait_for_size(8192)
        results = self.results(self.start_rotators(4))
        for status, result in results:
            with self.subTest(result=result.get("error_code", "success")):
                if result["status"] == "success":
                    self.assertEqual(status, 0)
                else:
                    self.assertEqual(status, 1)
                    self.assertEqual(result["error_code"], errors.ROTATION_IN_PROGRESS)
                    self.assertTrue(result["log_unchanged"])
        self.assertGreaterEqual(sum(r["status"] == "success" for _, r in results), 1)
        time.sleep(0.2)
        self.stop_writers()
        self.assertEveryLineOnce(self.line_counts())

    def test_different_logs_rotate_independently(self):
        """The rotation lock is per log: a busy apache log does not block the nginx log."""
        other = self.logs / "nginx_access.log"
        other.write_bytes(b"GET / 200\n" * 100)
        with locking.rotation_lock(self.log, timeout=0):
            busy = self.rotate()
            free = rotate.rotate_log("nginx_access", log_dir=self.logs, archive_dir=self.archives)
        self.assertEqual(busy["error_code"], errors.ROTATION_IN_PROGRESS)
        self.assertEqual(free["status"], "success", free)
        self.assertEqual(other.stat().st_size, 0)


class NonAppendWriterTest(StressTestCase):
    def test_writer_without_append_is_warned_about_and_leaves_a_hole(self):
        writer = self.start_writer("--no-append", rate=200)
        self.wait_for_size(16384)
        result = self.rotate()
        self.assertEqual(result["status"], "success", result)
        self.assertIn(f"pid {writer.pid} writes without O_APPEND", " ".join(result["warnings"]))
        self.assertEqual([h["append"] for h in result["open_by"]], [False])
        time.sleep(0.3)
        # The writer continued at its old offset: the start of the log is now a hole of zeros.
        size = os.stat(self.log).st_size
        self.assertGreater(size, result["original_size"])
        with open(self.log, "rb") as f:
            self.assertEqual(f.read(1024), b"\0" * 1024)


if __name__ == "__main__":
    unittest.main()
