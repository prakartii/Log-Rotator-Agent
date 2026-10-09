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
        name = Path(result["archive"]).name
        self.assertTrue(name.startswith("apache_error.log.2026-09."), name)
        self.assertFalse(name.endswith(".gz"))
        self.assertEqual(Path(result["archive"]).read_bytes(), self.data)
        self.assertEqual(self.log.read_bytes(), self.data)

    def test_archives_after_rotation(self):
        self.run_cli("rotate", "apache_error", "--label", "2026-09")
        status, out = self.run_cli("archives", "apache_error")
        self.assertEqual(status, 0)
        self.assertIn("1 archive(s)", out)
        self.assertIn("[2026-09]", out)

    def test_outside_path_is_refused(self):
        status, out = self.run_cli("rotate", "/etc/passwd")
        self.assertEqual(status, 1)
        self.assertIn("OUTSIDE_ALLOWED_DIR", out)
        self.assertIn("The log was not changed.", out)


class ToolsAndCallCommandTest(CliTestCase):
    def test_tools_prints_schemas(self):
        status, out = self.run_cli("tools")
        self.assertEqual(status, 0)
        names = [t["name"] for t in json.loads(out)]
        self.assertIn("rotate_log", names)

    def test_call_runs_a_tool_with_json_arguments(self):
        status, out = self.run_cli("call", "identify_log", '{"log": "apache error"}')
        self.assertEqual(status, 0)
        self.assertIn(f"-> {self.log} (matched by alias)", out)

    def test_call_with_invalid_json_is_a_usage_error(self):
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit) as exit_:
            self.run_cli("call", "identify_log", "{bad")
        self.assertEqual(exit_.exception.code, 2)
        self.assertIn("not valid JSON", err.getvalue())


class AskCommandTest(CliTestCase):
    def test_ask_rotates_and_says_what_it_understood(self):
        inode = os.stat(self.log).st_ino
        status, out = self.run_cli("ask", "rotate the apache error logs")
        self.assertEqual(status, 0, out)
        self.assertTrue(out.startswith("Understood: rotate_log(log='apache error')\n"), out)
        self.assertEqual(os.stat(self.log).st_size, 0)
        self.assertEqual(os.stat(self.log).st_ino, inode)

    def test_ask_without_quotes(self):
        status, result = self.run_json("ask", "what", "would", "happen", "if", "you", "rotated",
                                       "apache_error.log")
        self.assertEqual(status, 0, result)
        self.assertTrue(result["dry_run"])
        self.assertEqual(self.log.read_bytes(), self.data)

    def test_ask_not_understood_suggests_examples(self):
        status, out = self.run_cli("ask", "make me a sandwich")
        self.assertEqual(status, 1)
        self.assertIn("Try for example:", out)


class ProcessTest(CliTestCase):
    def test_runs_as_a_separate_process(self):
        """The master agent starts agent.py as a program; check the exit codes from outside."""
        base = [sys.executable, str(PROJECT_ROOT / "agent.py"), "--json",
                "--log-dir", str(self.logs), "--archive-dir", str(self.archives)]
        ok = subprocess.run(base + ["rotate", "apache_error"], capture_output=True, text=True, timeout=30)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        with gzip.open(json.loads(ok.stdout)["archive"], "rb") as f:
            self.assertEqual(f.read(), self.data)
        failed = subprocess.run(base + ["rotate", "no_such_log"], capture_output=True, text=True, timeout=30)
        self.assertEqual(failed.returncode, 1)
        self.assertEqual(json.loads(failed.stdout)["error_code"], "LOG_NOT_FOUND")
        usage = subprocess.run(base + ["explode"], capture_output=True, text=True, timeout=30)
        self.assertEqual(usage.returncode, 2)


if __name__ == "__main__":
    unittest.main()
