"""Tests for the agent's tool interface (log_rotator/agent_tools.py)."""

import gzip
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from log_rotator import agent_tools, errors


class DescribeToolsTest(unittest.TestCase):
    def test_all_tools_are_described(self):
        names = [t["name"] for t in agent_tools.describe_tools()]
        self.assertEqual(sorted(names), ["find_open_handles", "get_log_info", "identify_log",
                                         "list_archives", "list_logs", "rotate_log"])

    def test_descriptions_are_json_schemas(self):
        tools = json.loads(json.dumps(agent_tools.describe_tools()))
        for spec in tools:
            with self.subTest(tool=spec["name"]):
                self.assertTrue(spec["description"])
                self.assertEqual(spec["input_schema"]["type"], "object")
                self.assertFalse(spec["input_schema"]["additionalProperties"])


class ArgumentCheckTest(unittest.TestCase):
    def assertInvalid(self, result, message_part):
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_code"], errors.INVALID_REQUEST)
        self.assertIn(message_part, result["message"])

    def test_unknown_tool(self):
        result = agent_tools.call_tool("delete_everything")
        self.assertInvalid(result, "Unknown tool")
        self.assertIn("rotate_log", result["available_tools"])

    def test_unknown_argument(self):
        result = agent_tools.call_tool("rotate_log", {"log": "app", "path": "/etc/passwd"})
        self.assertInvalid(result, "Unknown argument(s) for rotate_log: path")

    def test_missing_required_argument(self):
        result = agent_tools.call_tool("identify_log", {})
        self.assertInvalid(result, "Missing argument(s)")
        self.assertEqual(result["missing"], ["log"])

    def test_wrong_argument_type(self):
        result = agent_tools.call_tool("rotate_log", {"dry_run": "yes"})
        self.assertInvalid(result, "must be a boolean")
        self.assertEqual(result["argument"], "dry_run")

    def test_boolean_is_not_a_number(self):
        result = agent_tools.call_tool("rotate_log", {"lock_timeout": True})
        self.assertInvalid(result, "must be a number")

    def test_negative_timeout(self):
        result = agent_tools.call_tool("rotate_log", {"watch_writer": -1})
        self.assertInvalid(result, "at least 0")

    def test_arguments_must_be_an_object(self):
        result = agent_tools.call_tool("list_logs", ["apache"])
        self.assertInvalid(result, "JSON object")


class ToolCallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.logs = base / "logs"
        self.archives = base / "rotated_logs"
        self.logs.mkdir()
        self.log = self.logs / "apache_error.log"
        self.data = b"".join(f"[error] request {i} failed\n".encode() for i in range(1000))
        self.log.write_bytes(self.data)
        (self.logs / "nginx_access.log").write_bytes(b"GET /\n")

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, name, arguments=None):
        return agent_tools.call_tool(name, arguments, log_dir=self.logs, archive_dir=self.archives)

    def test_list_logs(self):
        result = self.call("list_logs")
        self.assertEqual(result["status"], "success")
        self.assertEqual([log["name"] for log in result["logs"]], ["apache_error.log", "nginx_access.log"])

    def test_get_log_info_by_description(self):
        result = self.call("get_log_info", {"log": "the apache error logs"})
        self.assertEqual(result["status"], "success", result)
        self.assertEqual(result["log"], str(self.log))
        self.assertEqual(result["size"], len(self.data))
        self.assertEqual(result["inode"], os.stat(self.log).st_ino)

    def test_dry_run_changes_nothing(self):
        result = self.call("rotate_log", {"log": "apache_error", "dry_run": True})
        self.assertEqual(result["status"], "success", result)
        self.assertTrue(result["would_truncate"])
        self.assertEqual(self.log.read_bytes(), self.data)
        self.assertFalse(self.archives.exists())

    def test_rotate_archives_and_truncates(self):
        inode = os.stat(self.log).st_ino
        result = self.call("rotate_log", {"log": "apache error", "label": "2026-09"})
        self.assertEqual(result["status"], "success", result)
        self.assertIn(".2026-09.", result["archive"])
        self.assertEqual(os.stat(self.log).st_size, 0)
        self.assertEqual(os.stat(self.log).st_ino, inode)
        with gzip.open(result["archive"], "rb") as f:
            self.assertEqual(f.read(), self.data)

    def test_list_archives_after_rotation(self):
        self.call("rotate_log", {"log": "apache_error"})
        self.call("rotate_log", {"log": "nginx_access.log", "truncate": False})
        result = self.call("list_archives", {"log": "nginx access"})
        self.assertEqual(result["status"], "success", result)
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["archives"][0]["log_name"], "nginx_access.log")
        self.assertEqual(self.call("list_archives")["count"], 2)

    def test_find_open_handles_sees_our_descriptor_from_a_child(self):
        read_end, write_end = os.pipe()
        pid = os.fork()
        if pid == 0:  # child: hold the log open for appending until the parent is done
            os.close(read_end)
            os.open(self.log, os.O_WRONLY | os.O_APPEND)
            os.write(write_end, b"ready")
            time.sleep(10)
            os._exit(0)
        os.close(write_end)
        try:
            os.read(read_end, 5)
            result = self.call("find_open_handles", {"log": "apache_error"})
            self.assertEqual(result["status"], "success", result)
            self.assertEqual([h["pid"] for h in result["open_by"]], [pid])
            self.assertTrue(result["open_by"][0]["append"])
            self.assertEqual(result["warnings"], [])
        finally:
            os.kill(pid, 9)
            os.waitpid(pid, 0)
            os.close(read_end)

    def test_tool_errors_are_returned_not_raised(self):
        result = self.call("rotate_log", {"log": "mysql slow query"})
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_code"], errors.LOG_NOT_FOUND)
        self.assertEqual(result["action"], "rotate")


if __name__ == "__main__":
    unittest.main()
