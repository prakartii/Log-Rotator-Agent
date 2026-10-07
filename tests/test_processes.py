"""Tests for the /proc open-handle scanner (log_rotator/tools/processes.py).

Real writer.py subprocesses are started so that /proc really contains
descriptors pointing at the test log.
"""

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from log_rotator.tools import log_info, processes

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WRITER = PROJECT_ROOT / "writer.py"


@unittest.skipUnless(os.path.isdir("/proc"), "requires Linux /proc")
class ProcessesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.logs = Path(self.tmp.name)
        self.log = self.logs / "apache_error.log"
        self.log.write_text("")
        self.procs = []

    def tearDown(self):
        for proc in self.procs:
            if proc.poll() is None:
                proc.terminate()
            proc.wait(timeout=5)
        self.tmp.cleanup()

    def start_writer(self, *extra):
        proc = subprocess.Popen([sys.executable, str(WRITER), str(self.log), "--rate", "100", "--quiet", *extra],
                                cwd=PROJECT_ROOT, stdout=subprocess.DEVNULL)
        self.procs.append(proc)
        return proc

    def wait_for_handles(self, count, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            handles = processes.find_open_handles(self.log)
            if len(handles) >= count and all(h["offset"] > 0 for h in handles):
                return handles
            time.sleep(0.05)
        self.fail(f"expected {count} open handles, found {processes.find_open_handles(self.log)}")

    def test_no_handles_when_nobody_has_the_log_open(self):
        self.assertEqual(processes.find_open_handles(self.log), [])

    def test_finds_append_writer(self):
        proc = self.start_writer()
        [handle] = self.wait_for_handles(1)
        self.assertEqual(handle["pid"], proc.pid)
        self.assertEqual(handle["access"], "write")
        self.assertTrue(handle["append"])
        self.assertIn("writer.py", handle["command"])
        self.assertEqual(processes.unsafe_writers([handle]), [])

    def test_flags_writer_without_o_append_as_unsafe(self):
        safe = self.start_writer()
        unsafe = self.start_writer("--no-append")
        handles = self.wait_for_handles(2)
        self.assertEqual({h["pid"] for h in handles}, {safe.pid, unsafe.pid})
        self.assertEqual([h["pid"] for h in processes.unsafe_writers(handles)], [unsafe.pid])

    def test_own_descriptors_are_excluded_by_default(self):
        fd = os.open(self.log, os.O_RDONLY)
        try:
            self.assertEqual(processes.find_open_handles(self.log), [])
            [mine] = processes.find_open_handles(self.log, exclude_self=False)
            self.assertEqual((mine["pid"], mine["fd"], mine["access"]), (os.getpid(), fd, "read"))
        finally:
            os.close(fd)

    def test_reader_is_not_unsafe(self):
        handle = {"pid": 1, "fd": 3, "access": "read", "append": False, "offset": 10}
        self.assertEqual(processes.unsafe_writers([handle]), [])

    def test_get_log_info_reports_writers_and_warnings(self):
        unsafe = self.start_writer("--no-append")
        self.wait_for_handles(1)
        info = log_info.get_log_info(self.log, allowed_roots=[self.logs])
        self.assertEqual([h["pid"] for h in info["open_by"]], [unsafe.pid])
        self.assertEqual(len(info["warnings"]), 1)
        self.assertIn(f"pid {unsafe.pid}", info["warnings"][0])
        self.assertIn("O_APPEND", info["warnings"][0])

    def test_get_log_info_can_skip_process_scan(self):
        info = log_info.get_log_info(self.log, allowed_roots=[self.logs], include_processes=False)
        self.assertNotIn("open_by", info)


if __name__ == "__main__":
    unittest.main()
