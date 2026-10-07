"""Tests for path validation (log_rotator/tools/safety.py).

Each test builds a small fake filesystem in a temporary directory:

    tmp/
        logs/            <- the only allowed root
            app.log
        outside/
            secret.log   <- must never be touched
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from log_rotator import errors
from log_rotator.errors import RotatorError
from log_rotator.tools import safety


class SafetyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.logs = base / "logs"
        self.outside = base / "outside"
        self.logs.mkdir()
        self.outside.mkdir()
        self.log = self.logs / "app.log"
        self.log.write_text("hello\n")
        self.secret = self.outside / "secret.log"
        self.secret.write_text("do not touch\n")
        self.roots = [self.logs]

    def tearDown(self):
        os.chmod(self.log, 0o644)
        self.tmp.cleanup()

    def assertRejected(self, path, code, **kwargs):
        with self.assertRaises(RotatorError) as ctx:
            safety.validate_log_path(path, allowed_roots=self.roots, **kwargs)
        self.assertEqual(ctx.exception.code, code, ctx.exception.message)
        return ctx.exception

    # --- accepted ---------------------------------------------------------

    def test_valid_log_is_accepted(self):
        info = safety.validate_log_path(self.log, allowed_roots=self.roots)
        self.assertEqual(info["path"], str(self.log.resolve()))
        self.assertEqual(info["inode"], os.stat(self.log).st_ino)
        self.assertEqual(info["device"], os.stat(self.log).st_dev)

    def test_relative_path_inside_root_is_accepted(self):
        old_cwd = os.getcwd()
        try:
            os.chdir(self.logs)
            info = safety.validate_log_path("app.log", allowed_roots=self.roots)
        finally:
            os.chdir(old_cwd)
        self.assertEqual(info["path"], str(self.log.resolve()))

    # --- location ---------------------------------------------------------

    def test_file_outside_root_is_rejected(self):
        self.assertRejected(self.secret, errors.OUTSIDE_ALLOWED_DIR)

    def test_dotdot_traversal_is_rejected(self):
        self.assertRejected(self.logs / ".." / "outside" / "secret.log", errors.OUTSIDE_ALLOWED_DIR)

    def test_system_file_is_rejected(self):
        self.assertRejected("/etc/passwd", errors.OUTSIDE_ALLOWED_DIR)

    def test_symlinked_directory_pointing_outside_is_rejected(self):
        (self.logs / "sub").symlink_to(self.outside, target_is_directory=True)
        self.assertRejected(self.logs / "sub" / "secret.log", errors.OUTSIDE_ALLOWED_DIR)

    def test_root_directory_itself_is_rejected(self):
        self.assertRejected(self.logs, errors.OUTSIDE_ALLOWED_DIR)

    # --- file type --------------------------------------------------------

    def test_missing_file(self):
        self.assertRejected(self.logs / "nope.log", errors.LOG_NOT_FOUND)

    def test_empty_path(self):
        self.assertRejected("", errors.INVALID_REQUEST)
        self.assertRejected(None, errors.INVALID_REQUEST)

    def test_directory_is_rejected(self):
        (self.logs / "archive.d").mkdir()
        self.assertRejected(self.logs / "archive.d", errors.NOT_A_REGULAR_FILE)

    def test_fifo_is_rejected(self):
        fifo = self.logs / "pipe.log"
        os.mkfifo(fifo)
        self.assertRejected(fifo, errors.NOT_A_REGULAR_FILE)

    def test_symlink_to_outside_file_is_rejected(self):
        link = self.logs / "evil.log"
        link.symlink_to(self.secret)
        self.assertRejected(link, errors.SYMLINK_REJECTED)

    def test_symlink_to_inside_file_is_also_rejected(self):
        link = self.logs / "alias.log"
        link.symlink_to(self.log)
        self.assertRejected(link, errors.SYMLINK_REJECTED)

    def test_hard_link_is_rejected(self):
        os.link(self.secret, self.logs / "hard.log")  # same inode as secret.log
        err = self.assertRejected(self.logs / "hard.log", errors.HARDLINK_REJECTED)
        self.assertEqual(err.details["nlink"], 2)

    # --- permissions ------------------------------------------------------

    @unittest.skipIf(os.geteuid() == 0, "root bypasses file permissions")
    def test_read_only_file_rejected_for_write(self):
        os.chmod(self.log, 0o444)  # r--r--r--
        err = self.assertRejected(self.log, errors.PERMISSION_DENIED)
        self.assertEqual(err.details["mode"], "-r--r--r--")
        # Reading is still fine (e.g. for get_log_info).
        safety.validate_log_path(self.log, allowed_roots=self.roots, need_write=False)

    @unittest.skipIf(os.geteuid() == 0, "root bypasses file permissions")
    def test_unreadable_file_rejected(self):
        os.chmod(self.log, 0o000)
        self.assertRejected(self.log, errors.PERMISSION_DENIED, need_write=False)

    # --- open_validated ---------------------------------------------------

    def test_open_validated_returns_descriptor_for_same_inode(self):
        fd, info = safety.open_validated(self.log, os.O_RDWR, allowed_roots=self.roots)
        try:
            self.assertEqual(os.fstat(fd).st_ino, info["inode"])
            self.assertEqual(os.read(fd, 100), b"hello\n")
        finally:
            os.close(fd)

    def _validate_then(self, swap):
        """Run the real validation, then let `swap` change the file before open()."""
        real_validate = safety.validate_log_path

        def validate_and_swap(*args, **kwargs):
            info = real_validate(*args, **kwargs)
            swap()
            return info

        return mock.patch.object(safety, "validate_log_path", side_effect=validate_and_swap)

    def test_open_validated_detects_file_replaced_after_check(self):
        def replace_file():
            os.unlink(self.log)
            self.log.write_text("new file, new inode\n")

        with self._validate_then(replace_file):
            with self.assertRaises(RotatorError) as ctx:
                safety.open_validated(self.log, os.O_RDWR, allowed_roots=self.roots)
        self.assertEqual(ctx.exception.code, errors.FILE_CHANGED)

    def test_open_validated_refuses_symlink_swapped_in_after_check(self):
        def swap_for_symlink():
            os.unlink(self.log)
            self.log.symlink_to(self.secret)

        with self._validate_then(swap_for_symlink):
            with self.assertRaises(RotatorError) as ctx:
                safety.open_validated(self.log, os.O_RDWR, allowed_roots=self.roots)
        self.assertEqual(ctx.exception.code, errors.SYMLINK_REJECTED)
        self.assertEqual(self.secret.read_text(), "do not touch\n")


if __name__ == "__main__":
    unittest.main()
