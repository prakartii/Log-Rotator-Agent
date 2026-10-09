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


if __name__ == "__main__":
    unittest.main()
