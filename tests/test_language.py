"""Tests for natural-language request handling (log_rotator/language.py)."""

import unittest
from datetime import date

from log_rotator import errors
from log_rotator.errors import RotatorError
from log_rotator.language import parse_request

TODAY = date(2026, 10, 9)


def parse(text):
    return parse_request(text, today=TODAY)


class RotateRequestTest(unittest.TestCase):
    def test_rotate_by_description(self):
        result = parse("rotate the apache error logs")
        self.assertEqual(result["tool"], "rotate_log")
        self.assertEqual(result["arguments"], {"log": "apache error"})
        self.assertEqual(result["explanation"], "rotate_log(log='apache error')")

    def test_capitals_and_punctuation_are_ignored(self):
        self.assertEqual(parse("Rotate the Apache Error logs.")["arguments"], {"log": "apache error"})

    def test_last_month_becomes_the_archive_label(self):
        result = parse("Compress last month's nginx access log")
        self.assertEqual(result["arguments"], {"log": "nginx access", "label": "2026-09"})

    def test_last_month_in_january_is_december(self):
        result = parse_request("rotate last month's app log", today=date(2027, 1, 15))
        self.assertEqual(result["arguments"]["label"], "2026-12")

    def test_month_name_label(self):
        self.assertEqual(parse("rotate the app log for september")["arguments"]["label"], "2026-09")
        self.assertEqual(parse("rotate the app log for november")["arguments"]["label"], "2025-11")

    def test_explicit_label(self):
        result = parse("rotate the nginx error log, label it nightly")
        self.assertEqual(result["arguments"], {"log": "nginx error", "label": "nightly"})

    def test_keep_the_log_means_archive_only(self):
        for text in ("back up the nginx access log", "archive the nginx access log but keep the log",
                     "compress the nginx access log without truncating it"):
            with self.subTest(text=text):
                result = parse(text)
                self.assertEqual(result["tool"], "rotate_log")
                self.assertEqual(result["arguments"], {"log": "nginx access", "truncate": False})

    def test_without_compression(self):
        result = parse("truncate the app log but don't compress it")
        self.assertEqual(result["arguments"], {"log": "app", "compress": False})

    def test_dry_run_phrases(self):
        for text in ("what would happen if you rotated app.log?", "dry run: rotate app.log",
                     "simulate rotating app.log"):
            with self.subTest(text=text):
                self.assertEqual(parse(text)["arguments"], {"log": "app.log", "dry_run": True})

    def test_paths_keep_their_case(self):
        self.assertEqual(parse("please rotate logs/App.log")["arguments"], {"log": "logs/App.log"})

    def test_make_sure_the_writer_keeps_running(self):
        result = parse("rotate the apache log and make sure the writer keeps running")
        self.assertEqual(result["arguments"], {"log": "apache", "watch_writer": 3.0})


if __name__ == "__main__":
    unittest.main()
