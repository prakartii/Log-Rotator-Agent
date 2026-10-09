"""Tests for listing rotated archives (log_rotator/tools/archives.py)."""

import os
import tempfile
import unittest
from pathlib import Path

from log_rotator.tools.archives import list_archives, parse_archive_name


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


if __name__ == "__main__":
    unittest.main()
