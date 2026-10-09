"""Tests for natural-language request handling (log_rotator/language.py)."""

import gzip
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path

from log_rotator import errors
from log_rotator.errors import RotatorError
from log_rotator.language import handle_request, parse_request

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

    def test_wait_for_a_running_rotation(self):
        result = parse("rotate the app log, wait if another rotation is running")
        self.assertEqual(result["arguments"], {"log": "app", "lock_timeout": 10.0})

    def test_may_is_only_a_month_after_for(self):
        self.assertNotIn("label", parse("may I rotate the app log?")["arguments"])
        self.assertEqual(parse("rotate the app log for may")["arguments"]["label"], "2026-05")


class OtherRequestTest(unittest.TestCase):
    def test_list_logs(self):
        for text in ("list the logs", "show me all logs", "which log files are there?"):
            with self.subTest(text=text):
                self.assertEqual(parse(text), {"tool": "list_logs", "arguments": {},
                                               "explanation": "list_logs()"})

    def test_who_is_writing(self):
        result = parse("who is writing to the apache error log?")
        self.assertEqual(result["tool"], "find_open_handles")
        self.assertEqual(result["arguments"], {"log": "apache error"})

    def test_which_process_has_it_open(self):
        result = parse("which process has app.log open")
        self.assertEqual(result["arguments"], {"log": "app.log"})

    def test_list_archives(self):
        self.assertEqual(parse("show the archives of the apache error log")["arguments"],
                         {"log": "apache error"})
        result = parse("show rotated logs")
        self.assertEqual((result["tool"], result["arguments"]), ("list_archives", {}))

    def test_info_requests(self):
        for text in ("how big is the apache error log?", "show info about the apache error log",
                     "apache error"):
            with self.subTest(text=text):
                self.assertEqual(parse(text)["tool"], "get_log_info")
                self.assertEqual(parse(text)["arguments"], {"log": "apache error"})


class RefusedRequestTest(unittest.TestCase):
    def assertRefused(self, text, message_part):
        with self.assertRaises(RotatorError) as ctx:
            parse(text)
        self.assertEqual(ctx.exception.code, errors.INVALID_REQUEST)
        self.assertIn(message_part, ctx.exception.message)

    def test_rotate_everything_is_refused(self):
        for text in ("rotate everything", "rotate all logs", "compress every log"):
            with self.subTest(text=text):
                self.assertRefused(text, "one log at a time")

    def test_delete_is_refused(self):
        self.assertRefused("delete the apache log", "never deletes logs")
        self.assertRefused("rm logs/app.log", "never deletes logs")

    def test_rotate_without_a_log(self):
        self.assertRefused("rotate", "Which log should be rotated?")

    def test_empty_request(self):
        self.assertRefused("   ", "empty")

    def test_gibberish_lists_examples(self):
        with self.assertRaises(RotatorError) as ctx:
            parse("what's up?")
        self.assertIn("Could not understand", ctx.exception.message)
        self.assertIn("rotate the apache error logs", ctx.exception.details["examples"])


class HandleRequestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.logs = base / "logs"
        self.archives = base / "rotated_logs"
        self.logs.mkdir()
        self.log = self.logs / "nginx_access.log"
        self.data = b"GET /index.html 200\n" * 500
        self.log.write_bytes(self.data)

    def tearDown(self):
        self.tmp.cleanup()

    def handle(self, text):
        return handle_request(text, log_dir=self.logs, archive_dir=self.archives, today=TODAY)

    def test_request_is_carried_out(self):
        inode = os.stat(self.log).st_ino
        result = self.handle("compress last month's nginx access log")
        self.assertEqual(result["status"], "success", result)
        self.assertEqual(Path(result["archive"]).name[:len("nginx_access.log.2026-09.")],
                         "nginx_access.log.2026-09.")
        self.assertEqual(os.stat(self.log).st_size, 0)
        self.assertEqual(os.stat(self.log).st_ino, inode)
        with gzip.open(result["archive"], "rb") as f:
            self.assertEqual(f.read(), self.data)

    def test_result_shows_the_interpretation(self):
        result = self.handle("what would happen if you rotated the nginx access log?")
        self.assertEqual(result["request"], "what would happen if you rotated the nginx access log?")
        self.assertEqual(result["interpreted_as"]["explanation"],
                         "rotate_log(log='nginx access', dry_run=True)")
        self.assertEqual(self.log.read_bytes(), self.data)


if __name__ == "__main__":
    unittest.main()
