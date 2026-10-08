"""Tests for agent.py, the command-line entry point.

agent.py runs as a separate process, exactly as the master agent or a user
would start it. The LOG_ROTATOR_* environment variables point it at a
temporary directory.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
AGENT = PROJECT_ROOT / "agent.py"


class AgentCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.logs = base / "logs"
        self.archives = base / "rotated_logs"
        self.logs.mkdir()
        self.log = self.logs / "apache_error.log"
        self.data = b"".join(f"[error] request {i} failed\n".encode() for i in range(1000))
        self.log.write_bytes(self.data)
        self.env = dict(os.environ, LOG_ROTATOR_LOG_DIR=str(self.logs),
                        LOG_ROTATOR_ARCHIVE_DIR=str(self.archives))
        self.env.pop("LOG_ROTATOR_EXTRA_ROOTS", None)

    def tearDown(self):
        self.tmp.cleanup()

    def run_agent(self, *args, stdin=None):
        return subprocess.run([sys.executable, str(AGENT), *args], cwd=PROJECT_ROOT, env=self.env,
                              input=stdin, capture_output=True, text=True, timeout=30)

    def run_json(self, *args, stdin=None):
        run = self.run_agent(*args, stdin=stdin)
        return run.returncode, json.loads(run.stdout)

    def test_list(self):
        run = self.run_agent("list")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("apache_error.log", run.stdout)
        self.assertIn("1 log(s)", run.stdout)

    def test_info_accepts_unquoted_description(self):
        run = self.run_agent("info", "the", "apache", "error", "logs")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn(f"inode:      {os.stat(self.log).st_ino}", run.stdout)

    def test_rotate_dry_run_changes_nothing(self):
        run = self.run_agent("rotate", "apache_error", "--dry-run")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("DRY RUN", run.stdout)
        self.assertEqual(self.log.read_bytes(), self.data)
        self.assertFalse(self.archives.exists())

    def test_rotate_then_archives(self):
        inode = os.stat(self.log).st_ino
        code, result = self.run_json("rotate", "apache error", "--label", "2026-09", "--json")
        self.assertEqual(code, 0, result)
        self.assertTrue(result["inode_preserved"])
        self.assertEqual(os.stat(self.log).st_size, 0)
        self.assertEqual(os.stat(self.log).st_ino, inode)

        run = self.run_agent("archives")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn(os.path.basename(result["archive"]), run.stdout)

    def test_rotate_without_truncate_and_compression(self):
        code, result = self.run_json("--json", "rotate", "--no-truncate", "--no-compress")
        self.assertEqual(code, 0, result)
        self.assertFalse(result["truncated"])
        self.assertFalse(result["compressed"])
        self.assertEqual(Path(result["archive"]).read_bytes(), self.data)
        self.assertEqual(self.log.read_bytes(), self.data)

    def test_error_exit_code_and_message(self):
        run = self.run_agent("rotate", "nginx")
        self.assertEqual(run.returncode, 1)
        self.assertIn("LOG_NOT_FOUND", run.stderr)
        self.assertIn("apache_error.log", run.stderr)  # lists what is available
        self.assertEqual(self.log.read_bytes(), self.data)

    def test_rejects_path_outside_log_dir(self):
        code, result = self.run_json("rotate", "/etc/passwd", "--json")
        self.assertEqual(code, 1)
        self.assertEqual(result["error_code"], "OUTSIDE_ALLOWED_DIR")

    def test_bad_command_line_exits_2(self):
        self.assertEqual(self.run_agent("rotate", "--watch", "soon").returncode, 2)
        self.assertEqual(self.run_agent("explode").returncode, 2)

    def test_tools_prints_specs(self):
        code, specs = self.run_json("tools")
        self.assertEqual(code, 0)
        self.assertIn("rotate_log", [s["name"] for s in specs])

    def test_call_with_json_argument(self):
        code, result = self.run_json("call", "identify_log", '{"log": "apache error"}')
        self.assertEqual(code, 0, result)
        self.assertEqual(result["log"], str(self.log))

    def test_call_reads_arguments_from_stdin(self):
        code, result = self.run_json("call", "get_log_info", "-", stdin='{"log": "apache_error"}')
        self.assertEqual(code, 0, result)
        self.assertEqual(result["size"], len(self.data))

    def test_call_errors_are_json(self):
        code, result = self.run_json("call", "rotate_log", "{not json")
        self.assertEqual((code, result["error_code"]), (1, "INVALID_REQUEST"))
        code, result = self.run_json("call", "rotate_log", '{"dry_run": "yes"}')
        self.assertEqual((code, result["error_code"]), (1, "INVALID_REQUEST"))
        code, result = self.run_json("call", "rm_rf")
        self.assertEqual((code, result["error_code"]), (1, "INVALID_REQUEST"))
        self.assertEqual(self.log.read_bytes(), self.data)


if __name__ == "__main__":
    unittest.main()
