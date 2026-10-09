"""Tests for listing rotated archives (log_rotator/tools/archives.py)."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from log_rotator.tools.archives import list_archives, parse_archive_name, remove_stale_temp_files


class ParseArchiveNameTest(unittest.TestCase):
    def test_plain_gzip_archive(self):
        self.assertEqual(parse_archive_name("apache_error.log.2026-10-07T195312.gz"), {
            "log_name": "apache_error.log", "label": None, "rotated_at": "2026-10-07T19:53:12",
            "copy": 0, "compressed": True,
        })

    def test_label_and_copy_suffix(self):
        parts = parse_archive_name("apache_error.log.2026-09.2026-10-07T195312-2.gz")
        self.assertEqual(parts["log_name"], "apache_error.log")
        self.assertEqual(parts["label"], "2026-09")
        self.assertEqual(parts["copy"], 2)

    def test_uncompressed_archive(self):
        parts = parse_archive_name("app.log.2026-10-07T195312")
        self.assertEqual(parts["log_name"], "app.log")
        self.assertFalse(parts["compressed"])

    def test_unrelated_names_are_ignored(self):
        for name in ("notes.txt", "apache_error.log", "apache_error.log.2026-13-40T999999.gz"):
            with self.subTest(name=name):
                self.assertIsNone(parse_archive_name(name))


class ListArchivesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, name, data=b"x"):
        (self.dir / name).write_bytes(data)

    def test_lists_newest_first(self):
        self.make("apache_error.log.2026-10-01T100000.gz")
        self.make("apache_error.log.2026-10-07T195312.gz", b"abc")
        result = list_archives(self.dir)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["count"], 2)
        self.assertEqual([a["name"] for a in result["archives"]],
                         ["apache_error.log.2026-10-07T195312.gz", "apache_error.log.2026-10-01T100000.gz"])
        self.assertEqual(result["archives"][0]["size"], 3)
        self.assertEqual(result["total_size"], 4)

    def test_filter_by_log_name(self):
        self.make("apache_error.log.2026-10-01T100000.gz")
        self.make("nginx_access.log.2026-10-01T100000.gz")
        result = list_archives(self.dir, log_name="nginx_access.log")
        self.assertEqual([a["log_name"] for a in result["archives"]], ["nginx_access.log"])

    def test_hidden_temp_files_and_symlinks_are_skipped(self):
        self.make(".apache_error.log.2026-10-01T100000.gz.123.snapshot")
        self.make("apache_error.log.2026-10-01T100000.gz")
        os.symlink(self.dir / "apache_error.log.2026-10-01T100000.gz",
                   self.dir / "apache_error.log.2026-10-02T100000.gz")
        self.assertEqual(list_archives(self.dir)["count"], 1)

    def test_missing_directory_means_no_archives(self):
        result = list_archives(self.dir / "never_created")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["archives"], [])


class StaleTempFilesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def dead_pid(self):
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        return proc.pid

    def test_files_of_dead_rotators_are_removed(self):
        dead = self.dead_pid()
        names = [f".app.log.2026-10-09T100000.gz.{dead}.snapshot", f".app.log.2026-10-09T100000.gz.{dead}.tmp"]
        for name in names:
            (self.dir / name).write_bytes(b"partial")
        self.assertEqual(remove_stale_temp_files(self.dir), sorted(names))
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_files_of_running_rotators_and_archives_are_kept(self):
        keep = [f".app.log.2026-10-09T100000.gz.{os.getpid()}.snapshot", "app.log.2026-10-09T100000.gz",
                ".app.log.rotate.lock"]
        for name in keep:
            (self.dir / name).write_bytes(b"x")
        self.assertEqual(remove_stale_temp_files(self.dir), [])
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), sorted(keep))

    def test_missing_directory(self):
        self.assertEqual(remove_stale_temp_files(self.dir / "missing"), [])


if __name__ == "__main__":
    unittest.main()
