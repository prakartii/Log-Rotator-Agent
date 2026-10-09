"""Tests for the agent's tool interface (log_rotator/agent_tools.py)."""

import gzip
import json
import os
import tempfile
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


if __name__ == "__main__":
    unittest.main()
