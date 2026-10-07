"""Tests for log discovery and metadata (log_rotator/tools/log_info.py)."""

import os
import tempfile
import unittest
from pathlib import Path

from log_rotator import errors
from log_rotator.errors import RotatorError
from log_rotator.tools import log_info


class LogInfoTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.logs = Path(self.tmp.name) / "logs"
        self.logs.mkdir()
        self.apache = self.logs / "apache_error.log"
        self.apache.write_text("line 1\nline 2\n")
        (self.logs / "nginx_access.log").write_text("GET /\n")
        (self.logs / "app.log").write_text("")

    def tearDown(self):
        self.tmp.cleanup()

    def identify(self, query):
        return log_info.identify_log(query, log_dir=self.logs)


class IdentifyLogTest(LogInfoTestCase):
    def test_default_log_when_no_query(self):
        result = self.identify(None)
        self.assertEqual(result["log"], str(self.apache))
        self.assertEqual(result["matched_by"], "default")

    def test_exact_file_name(self):
        result = self.identify("nginx_access.log")
        self.assertEqual(result["log"], str(self.logs / "nginx_access.log"))
        self.assertEqual(result["matched_by"], "name")

    def test_name_without_extension(self):
        self.assertEqual(self.identify("nginx_access")["log"], str(self.logs / "nginx_access.log"))

    def test_alias_with_filler_words(self):
        result = self.identify("the Apache Error logs")
        self.assertEqual(result["log"], str(self.apache))
        self.assertEqual(result["matched_by"], "alias")

    def test_fuzzy_match(self):
        result = self.identify("nginx")
        self.assertEqual(result["log"], str(self.logs / "nginx_access.log"))
        self.assertEqual(result["matched_by"], "fuzzy")

    def test_explicit_path(self):
        result = self.identify(str(self.apache))
        self.assertEqual(result["matched_by"], "path")
        self.assertEqual(result["status"], "success")

    def test_ambiguous_name(self):
        (self.logs / "nginx_error.log").write_text("")
        with self.assertRaises(RotatorError) as ctx:
            self.identify("nginx")
        self.assertEqual(ctx.exception.code, errors.AMBIGUOUS_LOG)
        self.assertEqual(ctx.exception.details["candidates"], ["nginx_access.log", "nginx_error.log"])

    def test_unknown_log_lists_available_logs(self):
        with self.assertRaises(RotatorError) as ctx:
            self.identify("mysql slow query")
        self.assertEqual(ctx.exception.code, errors.LOG_NOT_FOUND)
        self.assertIn("apache_error.log", ctx.exception.details["available_logs"])

    def test_path_outside_log_dir_is_rejected(self):
        with self.assertRaises(RotatorError) as ctx:
            self.identify("/etc/passwd")
        self.assertEqual(ctx.exception.code, errors.OUTSIDE_ALLOWED_DIR)

    def test_symlinked_log_name_is_rejected(self):
        (self.logs / "evil.log").symlink_to("/etc/passwd")
        with self.assertRaises(RotatorError) as ctx:
            self.identify("evil.log")
        self.assertEqual(ctx.exception.code, errors.SYMLINK_REJECTED)


class ListLogsTest(LogInfoTestCase):
    def test_lists_regular_files_sorted(self):
        result = log_info.list_logs(self.logs)
        self.assertEqual(result["status"], "success")
        self.assertEqual([l["name"] for l in result["logs"]],
                         ["apache_error.log", "app.log", "nginx_access.log"])
        self.assertEqual(result["count"], 3)
        apache = result["logs"][0]
        self.assertEqual(apache["size"], len("line 1\nline 2\n"))
        self.assertEqual(apache["inode"], os.stat(self.apache).st_ino)

    def test_skips_symlinks_hidden_files_and_directories(self):
        (self.logs / "link.log").symlink_to(self.apache)
        (self.logs / ".lock").write_text("")
        (self.logs / "old").mkdir()
        names = [l["name"] for l in log_info.list_logs(self.logs)["logs"]]
        self.assertEqual(names, ["apache_error.log", "app.log", "nginx_access.log"])

    def test_missing_directory(self):
        with self.assertRaises(RotatorError) as ctx:
            log_info.list_logs(self.logs / "missing")
        self.assertEqual(ctx.exception.code, errors.LOG_NOT_FOUND)


class GetLogInfoTest(LogInfoTestCase):
    def info(self, path):
        return log_info.get_log_info(path, allowed_roots=[self.logs])

    def test_metadata_matches_stat(self):
        os.chmod(self.apache, 0o640)
        result = self.info(self.apache)
        st = os.stat(self.apache)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["action"], "get_log_info")
        self.assertEqual(result["inode"], st.st_ino)
        self.assertEqual(result["device"], st.st_dev)
        self.assertEqual(result["size"], st.st_size)
        self.assertEqual(result["nlink"], 1)
        self.assertEqual(result["uid"], os.getuid())
        self.assertEqual(result["mode"], "-rw-r-----")
        self.assertEqual(result["mode_octal"], "0o640")
        self.assertTrue(result["readable"])
        self.assertTrue(result["writable"])
        self.assertFalse(result["sparse"])

    def test_detects_sparse_file(self):
        # Write one byte at offset 1 MiB: everything before it is a hole.
        fd = os.open(self.apache, os.O_WRONLY)
        os.lseek(fd, 1024 * 1024, os.SEEK_SET)
        os.write(fd, b"x")
        os.close(fd)
        result = self.info(self.apache)
        self.assertEqual(result["size"], 1024 * 1024 + 1)
        self.assertTrue(result["sparse"])

    def test_human_size(self):
        self.assertEqual(log_info.human_size(500), "500 B")
        self.assertEqual(log_info.human_size(1536), "1.5 KiB")
        self.assertEqual(log_info.human_size(50 * 1024 * 1024), "50.0 MiB")

    def test_missing_log(self):
        with self.assertRaises(RotatorError) as ctx:
            self.info(self.logs / "nope.log")
        self.assertEqual(ctx.exception.code, errors.LOG_NOT_FOUND)

    @unittest.skipIf(os.geteuid() == 0, "root bypasses file permissions")
    def test_read_only_log_still_reports_info(self):
        os.chmod(self.apache, 0o444)
        result = self.info(self.apache)
        self.assertTrue(result["readable"])
        self.assertFalse(result["writable"])


if __name__ == "__main__":
    unittest.main()
