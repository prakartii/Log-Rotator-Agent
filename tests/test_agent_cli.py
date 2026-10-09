"""Tests for the command-line interface (agent.py)."""

import contextlib
import gzip
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import agent  # noqa: E402  (agent.py lives in the project root)


class CliTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.logs = base / "logs"
        self.archives = base / "rotated_logs"
        self.logs.mkdir()
        self.log = self.logs / "apache_error.log"
        self.data = b"".join(f"[error] request {i} failed\n".encode() for i in range(1000))
        self.log.write_bytes(self.data)

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *argv):
        """Run agent.main() in-process; return (exit status, stdout)."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = agent.main(["--log-dir", str(self.logs), "--archive-dir", str(self.archives), *argv])
        return status, out.getvalue()

    def run_json(self, *argv):
        status, out = self.run_cli("--json", *argv)
        return status, json.loads(out)


class ListCommandTest(CliTestCase):
    def test_list_prints_each_log(self):
        status, out = self.run_cli("list")
        self.assertEqual(status, 0)
        self.assertIn("1 log(s)", out)
        self.assertIn("apache_error.log", out)

    def test_list_json_is_the_tool_result(self):
        status, result = self.run_json("list")
        self.assertEqual(status, 0)
        self.assertEqual(result["action"], "list_logs")
        self.assertEqual(result["logs"][0]["size"], len(self.data))

    def test_empty_log_directory(self):
        self.log.unlink()
        status, out = self.run_cli("list")
        self.assertEqual(status, 0)
        self.assertIn("No logs in", out)


class InfoCommandTest(CliTestCase):
    def test_info_shows_inode_and_size(self):
        status, out = self.run_cli("info", "the apache error logs")
        self.assertEqual(status, 0)
        self.assertIn(f"inode {os.stat(self.log).st_ino}", out)
        self.assertIn(f"({len(self.data)} bytes)", out)
        self.assertIn("open by: no process", out)

    def test_unknown_log_exits_1_and_lists_available_logs(self):
        status, out = self.run_cli("info", "mysql")
        self.assertEqual(status, 1)
        self.assertIn("ERROR LOG_NOT_FOUND", out)
        self.assertIn("Available logs: apache_error.log", out)


class RotateCommandTest(CliTestCase):
    def test_dry_run(self):
        status, out = self.run_cli("rotate", "apache_error", "--dry-run")
        self.assertEqual(status, 0)
        self.assertIn("DRY RUN: nothing was changed.", out)
        self.assertEqual(self.log.read_bytes(), self.data)
        self.assertFalse(self.archives.exists())

    def test_rotate_keeps_the_inode(self):
        inode = os.stat(self.log).st_ino
        status, out = self.run_cli("rotate", "apache error")
        self.assertEqual(status, 0, out)
        self.assertIn(f"inode:    {inode} -> {inode} (preserved)", out)
        self.assertEqual(os.stat(self.log).st_size, 0)
        self.assertEqual(os.stat(self.log).st_ino, inode)

    def test_rotate_options_reach_the_tool(self):
        status, result = self.run_json("rotate", "apache_error", "--no-compress", "--no-truncate",
                                       "--label", "2026-09")
        self.assertEqual(status, 0, result)
        self.assertFalse(result["compressed"])
        self.assertFalse(result["truncated"])
        self.assertTrue(result["archive"].endswith(".log.2026-09." + Path(result["archive"]).name.split(".")[-1]))
        self.assertEqual(Path(result["archive"]).read_bytes(), self.data)
        self.assertEqual(self.log.read_bytes(), self.data)


if __name__ == "__main__":
    unittest.main()
