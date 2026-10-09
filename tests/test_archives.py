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


if __name__ == "__main__":
    unittest.main()
