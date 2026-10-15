"""Tests for date-range parsing and the period argument of the tools."""

import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from log_rotator import agent_tools, errors
from log_rotator.tools import periods
from log_rotator.tools.archives import list_archives
from log_rotator.tools.log_info import list_logs

NOW = datetime(2026, 10, 15, 14, 30)  # a Thursday


class ParsePeriodTests(unittest.TestCase):
    def check(self, text, start, end, label):
        p = periods.parse_period(text, NOW)
        self.assertEqual((p.start, p.end, p.label), (start, end, label), text)

    def test_months(self):
        self.check("last month", datetime(2026, 9, 1), datetime(2026, 10, 1), "2026-09")
        self.check("this month", datetime(2026, 10, 1), datetime(2026, 11, 1), "2026-10")
        self.check("2026-02", datetime(2026, 2, 1), datetime(2026, 3, 1), "2026-02")

    def test_last_month_across_new_year(self):
        p = periods.parse_period("last month", datetime(2026, 1, 3))
        self.assertEqual((p.start, p.end, p.label),
                         (datetime(2025, 12, 1), datetime(2026, 1, 1), "2025-12"))
        p = periods.parse_period("2025-12", NOW)
        self.assertEqual(p.end, datetime(2026, 1, 1))

    def test_days_and_weeks(self):
        self.check("today", datetime(2026, 10, 15), datetime(2026, 10, 16), "2026-10-15")
        self.check("yesterday", datetime(2026, 10, 14), datetime(2026, 10, 15), "2026-10-14")
        self.check("this week", datetime(2026, 10, 12), datetime(2026, 10, 19), "2026-10-12_2026-10-18")
        self.check("last week", datetime(2026, 10, 5), datetime(2026, 10, 12), "2026-10-05_2026-10-11")
        self.check("last 7 days", datetime(2026, 10, 9), datetime(2026, 10, 16), "2026-10-09_2026-10-15")
        self.check("2026-09-15", datetime(2026, 9, 15), datetime(2026, 9, 16), "2026-09-15")

    def test_years(self):
        self.check("last year", datetime(2025, 1, 1), datetime(2026, 1, 1), "2025")
        self.check("2026", datetime(2026, 1, 1), datetime(2027, 1, 1), "2026")

    def test_case_spacing_and_filler(self):
        self.check("  From  LAST   Month ", datetime(2026, 9, 1), datetime(2026, 10, 1), "2026-09")
        self.check("last_month", datetime(2026, 9, 1), datetime(2026, 10, 1), "2026-09")

    def test_labels_are_valid_archive_labels(self):
        for text in ("last month", "last week", "last 3 days", "2026-09-15", "last year"):
            self.assertRegex(periods.parse_period(text, NOW).label, r"^[A-Za-z0-9_-]+$")

    def test_bad_input(self):
        for text in ("", "   ", None, 5, "someday", "2026-13", "2026-02-30", "last 0 days",
                     "last month; rm -rf /", "0000-01"):
            with self.assertRaises(errors.RotatorError, msg=repr(text)) as ctx:
                periods.parse_period(text, NOW)
            self.assertEqual(ctx.exception.code, errors.INVALID_REQUEST)

    def test_contains_is_half_open(self):
        p = periods.parse_period("last month", NOW)
        self.assertTrue(p.contains(datetime(2026, 9, 1).timestamp()))
        self.assertTrue(p.contains(datetime(2026, 9, 30, 23, 59, 59).timestamp()))
        self.assertFalse(p.contains(datetime(2026, 10, 1).timestamp()))
        self.assertFalse(p.contains(datetime(2026, 8, 31, 23, 59, 59).timestamp()))


class PeriodToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.logs, self.archives = base / "logs", base / "rotated_logs"
        self.logs.mkdir()
        self.archives.mkdir()
        for name, when in (("apache_error.log", datetime(2026, 9, 10)),
                           ("nginx_error.log", datetime(2026, 10, 2)),
                           ("app.log", datetime(2026, 8, 31, 23, 59))):
            path = self.logs / name
            path.write_bytes(b"line\n" * 100)
            os.utime(path, (when.timestamp(), when.timestamp()))
        # Archive times come from the stamp in the name; the mtime is deliberately "now".
        for name in ("apache_error.log.2026-09.2026-09-30T120000.gz",
                     "apache_error.log.2026-10-14T120000.gz"):
            (self.archives / name).write_bytes(b"x")

    def call(self, tool, arguments=None):
        return agent_tools.call_tool(tool, arguments, log_dir=self.logs, archive_dir=self.archives)

    def names(self, result, key):
        return sorted(item["name"] for item in result[key])

    def test_list_logs_by_period(self):
        result = list_logs(self.logs, period="2026-09", now=NOW)
        self.assertEqual(self.names(result, "logs"), ["apache_error.log"])
        self.assertEqual(result["period"]["label"], "2026-09")
        self.assertEqual(list_logs(self.logs, period="2026-08", now=NOW)["count"], 1)
        self.assertEqual(list_logs(self.logs)["count"], 3)
        self.assertNotIn("period", list_logs(self.logs))

    def test_list_archives_by_period_uses_the_name_stamp(self):
        result = list_archives(self.archives, period="2026-10", now=NOW)
        self.assertEqual(self.names(result, "archives"), ["apache_error.log.2026-10-14T120000.gz"])
        result = self.call("list_archives", {"period": "2026-09"})
        self.assertEqual(self.names(result, "archives"),
                         ["apache_error.log.2026-09.2026-09-30T120000.gz"])
        self.assertEqual(self.call("list_archives")["count"], 2)

    def test_period_filter_through_the_tool_interface(self):
        result = self.call("list_logs", {"period": "2026-09"})
        self.assertEqual(self.names(result, "logs"), ["apache_error.log"])

    def test_bad_period_is_a_structured_error(self):
        for tool in ("list_logs", "list_archives", "rotate_log"):
            result = self.call(tool, {"period": "whenever"})
            self.assertEqual(result["status"], "error", tool)
            self.assertEqual(result["error_code"], errors.INVALID_REQUEST, tool)
            self.assertIn("supported_periods", result)
        self.assertEqual((self.logs / "apache_error.log").stat().st_size, 500)
        self.assertEqual(len(list(self.archives.iterdir())), 2)

    def test_rotate_period_labels_archive_and_reports_range(self):
        result = self.call("rotate_log", {"log": "apache_error", "period": "2026-09"})
        self.assertEqual(result["status"], "success", result)
        self.assertIn(".2026-09.", os.path.basename(result["archive"]))
        self.assertEqual(result["period"]["start"], "2026-09-01T00:00:00")
        self.assertTrue(result["truncated"])

    def test_explicit_label_wins_over_period(self):
        result = self.call("rotate_log", {"log": "apache_error", "period": "2026-09",
                                          "label": "audit"})
        self.assertIn(".audit.", os.path.basename(result["archive"]))
        self.assertEqual(result["period"]["label"], "2026-09")

    def test_dry_run_with_period_changes_nothing(self):
        result = self.call("rotate_log", {"log": "apache_error", "period": "last month",
                                          "dry_run": True})
        self.assertEqual(result["status"], "success", result)
        self.assertIn("period", result)
        self.assertEqual(len(list(self.archives.iterdir())), 2)
        self.assertEqual((self.logs / "apache_error.log").stat().st_size, 500)

    def test_period_must_be_a_string(self):
        result = self.call("list_logs", {"period": 5})
        self.assertEqual(result["error_code"], errors.INVALID_REQUEST)


if __name__ == "__main__":
    unittest.main()
