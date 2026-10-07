"""Tests for post-rotation verification (verifier.verify_rotation)."""

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from log_rotator import errors
from log_rotator.errors import RotatorError
from log_rotator.tools import verifier

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class VerifyRotationTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Path(self.tmp.name) / "apache_error.log"
        self.log.write_bytes(b"old line\n" * 1000)
        os.chmod(self.log, 0o644)
        self.before = os.stat(self.log)

    def tearDown(self):
        if self.log.exists() and not self.log.is_symlink():
            os.chmod(self.log, 0o644)
        self.tmp.cleanup()

    def truncate_in_place(self):
        fd = os.open(self.log, os.O_RDWR)
        os.ftruncate(fd, 0)
        os.close(fd)

    def assertRotationFails(self, failed_check, **kwargs):
        with self.assertRaises(RotatorError) as ctx:
            verifier.verify_rotation(self.log, self.before, **kwargs)
        self.assertEqual(ctx.exception.code, errors.ROTATION_VERIFY_FAILED)
        self.assertFalse(ctx.exception.details["checks"][failed_check], ctx.exception.details["checks"])
        return ctx.exception


class SuccessfulVerificationTest(VerifyRotationTestCase):
    def test_truncated_log_passes_every_check(self):
        self.truncate_in_place()
        result = verifier.verify_rotation(self.log, self.before)
        self.assertEqual(result["status"], "success")
        self.assertTrue(result["verified"])
        self.assertEqual(result["inode"], self.before.st_ino)
        self.assertEqual(result["size"], 0)
        self.assertEqual(result["checks"], {
            "exists": True, "regular_file": True, "same_inode": True, "not_deleted": True,
            "mode_preserved": True, "owner_preserved": True, "appendable": True,
        })

    def test_appendable_check_adds_no_data(self):
        self.truncate_in_place()
        verifier.verify_rotation(self.log, self.before)
        self.assertEqual(self.log.read_bytes(), b"")

    def test_log_that_already_grew_again_still_passes(self):
        self.truncate_in_place()
        with open(self.log, "ab") as f:
            f.write(b"new line after rotation\n")
        result = verifier.verify_rotation(self.log, self.before)
        self.assertEqual(result["size"], 24)

    def test_no_writer_check_without_writer_pids(self):
        self.truncate_in_place()
        result = verifier.verify_rotation(self.log, self.before)
        self.assertNotIn("writers_attached", result["checks"])
        self.assertEqual(result["writers_attached"], [])


class FailedVerificationTest(VerifyRotationTestCase):
    def test_deleted_log_fails(self):
        self.log.unlink()
        self.assertRotationFails("exists")

    def test_delete_and_recreate_fails_on_inode(self):
        """Exactly the unsafe rotation style this project avoids."""
        self.log.unlink()
        self.log.write_bytes(b"")
        err = self.assertRotationFails("same_inode")
        self.assertEqual(err.details["inode_before"], self.before.st_ino)
        self.assertNotEqual(err.details["inode_after"], self.before.st_ino)

    def test_rename_away_and_recreate_fails(self):
        os.rename(self.log, self.log.with_suffix(".1"))  # classic "mv + create" rotation
        self.log.write_bytes(b"")
        self.assertRotationFails("same_inode")

    def test_symlink_planted_at_log_path_fails(self):
        target = self.log.with_suffix(".real")
        os.rename(self.log, target)
        self.log.symlink_to(target)
        self.assertRotationFails("regular_file")

    def test_directory_at_log_path_fails(self):
        self.log.unlink()
        self.log.mkdir()
        try:
            self.assertRotationFails("regular_file")
        finally:
            self.log.rmdir()

    def test_changed_permissions_fail(self):
        os.chmod(self.log, 0o666)
        err = self.assertRotationFails("mode_preserved")
        self.assertEqual(err.details["mode_before"], "-rw-r--r--")
        self.assertEqual(err.details["mode_after"], "-rw-rw-rw-")

    @unittest.skipIf(os.geteuid() == 0, "root can always write")
    def test_unwritable_log_fails_appendable(self):
        os.chmod(self.log, 0o444)
        before = os.stat(self.log)  # same mode before and after, so only 'appendable' fails
        with self.assertRaises(RotatorError) as ctx:
            verifier.verify_rotation(self.log, before)
        self.assertFalse(ctx.exception.details["checks"]["appendable"])


if __name__ == "__main__":
    unittest.main()
