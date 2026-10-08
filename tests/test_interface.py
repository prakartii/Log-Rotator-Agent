"""Tests for the master agent's tool interface (log_rotator/interface.py)."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from log_rotator import config, errors, interface


class InterfaceTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.logs = base / "logs"
        self.archives = base / "rotated_logs"
        self.logs.mkdir()
        self.log = self.logs / "apache_error.log"
        self.data = b"".join(f"[error] request {i} failed\n".encode() for i in range(1000))
        self.log.write_bytes(self.data)
        # Point the agent at the temporary directories, like LOG_ROTATOR_* env vars would.
        for name, value in (("LOG_DIR", self.logs), ("ARCHIVE_DIR", self.archives),
                            ("ALLOWED_LOG_ROOTS", [self.logs])):
            patcher = mock.patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def assertError(self, result, code):
        self.assertEqual(result["status"], "error", result)
        self.assertEqual(result["error_code"], code, result)


class ToolSpecsTest(InterfaceTestCase):
    def test_every_tool_has_a_json_schema(self):
        specs = interface.tool_specs()
        self.assertEqual([s["name"] for s in specs], list(interface.TOOLS))
        for spec in specs:
            self.assertTrue(spec["description"])
            self.assertEqual(spec["parameters"]["type"], "object")
            self.assertFalse(spec["parameters"]["additionalProperties"])
        json.dumps(specs)  # must be sendable as JSON

    def test_directories_are_not_arguments(self):
        """A request must never be able to redirect the agent to another directory."""
        for spec in interface.tool_specs():
            for arg in spec["parameters"]["properties"]:
                self.assertNotIn("dir", arg)
                self.assertNotIn("root", arg)


class CallToolTest(InterfaceTestCase):
    def test_list_logs(self):
        result = interface.call_tool("list_logs")
        self.assertEqual(result["status"], "success")
        self.assertEqual([log["name"] for log in result["logs"]], ["apache_error.log"])

    def test_identify_by_description(self):
        result = interface.call_tool("identify_log", {"log": "the apache error logs"})
        self.assertEqual(result["log"], str(self.log))

    def test_identify_default_log(self):
        self.assertEqual(interface.call_tool("identify_log", {})["log"], str(self.log))
        self.assertEqual(interface.call_tool("identify_log", {"log": None})["log"], str(self.log))

    def test_get_log_info_resolves_alias(self):
        result = interface.call_tool("get_log_info", {"log": "apache error"})
        self.assertEqual(result["status"], "success", result)
        self.assertEqual(result["size"], len(self.data))
        self.assertEqual(result["inode"], os.stat(self.log).st_ino)

    def test_rotate_dry_run_changes_nothing(self):
        result = interface.call_tool("rotate_log", {"log": "apache_error", "dry_run": True})
        self.assertEqual(result["status"], "success", result)
        self.assertTrue(result["dry_run"])
        self.assertEqual(self.log.read_bytes(), self.data)
        self.assertFalse(self.archives.exists())

    def test_rotate_and_list_archives(self):
        inode = os.stat(self.log).st_ino
        result = interface.call_tool("rotate_log", {"log": "apache_error", "label": "2026-09"})
        self.assertEqual(result["status"], "success", result)
        self.assertEqual(os.stat(self.log).st_size, 0)
        self.assertEqual(os.stat(self.log).st_ino, inode)
        self.assertIn(".2026-09.", result["archive"])

        listed = interface.call_tool("list_archives", {"log": "apache_error"})
        self.assertEqual(listed["count"], 1)
        self.assertEqual(listed["archives"][0]["path"], result["archive"])
        self.assertTrue(listed["archives"][0]["compressed"])

    def test_list_archives_newest_first_and_filtered(self):
        self.archives.mkdir()
        for name, mtime in (("apache_error.log.a.gz", 100), ("apache_error.log.b.gz", 200),
                            ("app.log.c.gz", 300), (".apache_error.log.tmp", 400)):
            (self.archives / name).write_bytes(b"x")
            os.utime(self.archives / name, (mtime, mtime))
        everything = interface.call_tool("list_archives")
        self.assertEqual([a["name"] for a in everything["archives"]],
                         ["app.log.c.gz", "apache_error.log.b.gz", "apache_error.log.a.gz"])
        only_apache = interface.call_tool("list_archives", {"log": "apache_error"})
        self.assertEqual([a["name"] for a in only_apache["archives"]],
                         ["apache_error.log.b.gz", "apache_error.log.a.gz"])

    def test_list_archives_before_any_rotation(self):
        result = interface.call_tool("list_archives")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["count"], 0)

    def test_tool_errors_become_results(self):
        self.assertError(interface.call_tool("get_log_info", {"log": "nginx"}), errors.LOG_NOT_FOUND)
        self.assertError(interface.call_tool("rotate_log", {"log": "/etc/passwd"}),
                         errors.OUTSIDE_ALLOWED_DIR)
        result = interface.call_tool("rotate_log", {"log": "apache_error", "label": "../x"})
        self.assertError(result, errors.INVALID_REQUEST)
        self.assertEqual(self.log.read_bytes(), self.data)

    def test_os_errors_become_results(self):
        with mock.patch.object(interface.log_info, "list_logs",
                               side_effect=PermissionError(13, "Permission denied", str(self.logs))):
            self.assertError(interface.call_tool("list_logs"), errors.PERMISSION_DENIED)


class ArgumentValidationTest(InterfaceTestCase):
    def assertRejected(self, name, arguments):
        result = interface.call_tool(name, arguments)
        self.assertError(result, errors.INVALID_REQUEST)
        self.assertEqual(result["action"], name)
        self.assertEqual(self.log.read_bytes(), self.data, "nothing may run on a bad request")
        return result

    def test_unknown_tool(self):
        result = self.assertRejected("delete_log", {})
        self.assertIn("rotate_log", result["available_tools"])

    def test_unknown_argument(self):
        result = self.assertRejected("rotate_log", {"log": "apache_error", "archive_dir": "/tmp"})
        self.assertEqual(result["unknown_arguments"], ["archive_dir"])

    def test_arguments_must_be_an_object(self):
        self.assertRejected("rotate_log", ["apache_error"])

    def test_wrong_types(self):
        self.assertRejected("rotate_log", {"dry_run": "yes"})
        self.assertRejected("rotate_log", {"log": 5})
        self.assertRejected("rotate_log", {"watch_writer": True})  # bool is not a number
        self.assertRejected("rotate_log", {"watch_writer": -1})
        self.assertRejected("rotate_log", {"lock_timeout": float("inf")})
        self.assertRejected("rotate_log", {"watch_writer": float("nan")})

    def test_null_means_default(self):
        result = interface.call_tool("rotate_log", {"log": None, "dry_run": True, "label": None,
                                                    "lock_timeout": None})
        self.assertEqual(result["status"], "success", result)
        self.assertEqual(result["log"], str(self.log))


if __name__ == "__main__":
    unittest.main()
