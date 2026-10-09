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


if __name__ == "__main__":
    unittest.main()
