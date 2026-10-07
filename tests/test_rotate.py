"""Tests for the rotation pipeline (log_rotator/rotate.py)."""

import gzip
import hashlib
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from log_rotator import errors, rotate
from log_rotator.errors import RotatorError

NOW = datetime(2026, 10, 7, 19, 53, 12)
ARCHIVE_NAME = "apache_error.log.2026-10-07T195312.gz"


class RotateTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.logs = base / "logs"
        self.archives = base / "rotated_logs"
        self.logs.mkdir()
        self.log = self.logs / "apache_error.log"
        self.data = b"".join(f"[error] request {i} failed\n".encode() for i in range(20000))
        self.log.write_bytes(self.data)
        self.inode = os.stat(self.log).st_ino

    def tearDown(self):
        os.chmod(self.log, 0o644)
        self.tmp.cleanup()

    def rotate(self, log="apache_error", **kwargs):
        kwargs.setdefault("log_dir", self.logs)
        kwargs.setdefault("archive_dir", self.archives)
        kwargs.setdefault("now", NOW)
        return rotate.rotate_log(log, **kwargs)

    def archive_files(self):
        return sorted(p.name for p in self.archives.iterdir()) if self.archives.exists() else []

    def assertLogUnchanged(self, result):
        self.assertEqual(result["status"], "error")
        self.assertFalse(result["truncated"])
        self.assertTrue(result["log_unchanged"])
        self.assertEqual(self.log.read_bytes(), self.data, "log content must be untouched")
        self.assertEqual(os.stat(self.log).st_ino, self.inode)


class SuccessfulRotationTest(RotateTestCase):
    def test_rotation_archives_and_truncates_in_place(self):
        result = self.rotate()
        self.assertEqual(result["status"], "success", result)
        self.assertTrue(result["truncated"])
        self.assertEqual(os.stat(self.log).st_size, 0)
        self.assertTrue(self.log.exists())
        self.assertEqual(os.stat(self.log).st_ino, self.inode)
        with gzip.open(result["archive"], "rb") as f:
            self.assertEqual(f.read(), self.data)

    def test_result_schema(self):
        result = self.rotate()
        self.assertEqual(result["action"], "rotate")
        self.assertEqual(result["log"], str(self.log))
        self.assertEqual(result["archive"], str(self.archives / ARCHIVE_NAME))
        self.assertEqual(result["original_size"], len(self.data))
        self.assertEqual(result["bytes_archived"], len(self.data))
        self.assertEqual(result["archive_size"], os.stat(result["archive"]).st_size)
        self.assertEqual(result["sha256"], hashlib.sha256(self.data).hexdigest())
        self.assertEqual(result["inode_before"], self.inode)
        self.assertEqual(result["inode_after"], self.inode)
        self.assertTrue(result["inode_preserved"])
        self.assertEqual(result["size_after"], 0)
        self.assertFalse(result["dry_run"])
        self.assertEqual(result["open_by"], [])
        self.assertEqual(result["warnings"], [])

    def test_steps_run_in_safe_order(self):
        result = self.rotate()
        self.assertEqual([s["step"] for s in result["steps"]],
                         ["identify", "open", "snapshot", "compress", "verify_archive", "truncate"])
        self.assertTrue(all(s["ok"] for s in result["steps"]))
        self.assertTrue(all(s["ms"] >= 0 for s in result["steps"]))

    def test_no_temporary_files_left(self):
        self.rotate()
        self.assertEqual(self.archive_files(), [ARCHIVE_NAME])

    def test_writer_descriptor_continues_after_rotation(self):
        writer = os.open(self.log, os.O_WRONLY | os.O_APPEND)
        try:
            self.rotate()
            os.write(writer, b"first line after rotation\n")
        finally:
            os.close(writer)
        self.assertEqual(self.log.read_bytes(), b"first line after rotation\n")

    def test_archive_only_leaves_log_alone(self):
        result = self.rotate(truncate=False)
        self.assertEqual(result["status"], "success")
        self.assertFalse(result["truncated"])
        self.assertEqual(self.log.read_bytes(), self.data)
        self.assertNotIn("truncate", [s["step"] for s in result["steps"]])
        self.assertEqual(self.archive_files(), [ARCHIVE_NAME])

    def test_dry_run_changes_nothing(self):
        result = self.rotate(dry_run=True)
        self.assertEqual(result["status"], "success")
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["would_archive_to"], str(self.archives / ARCHIVE_NAME))
        self.assertTrue(result["would_truncate"])
        self.assertFalse(result["truncated"])
        self.assertEqual(self.log.read_bytes(), self.data)
        self.assertFalse(self.archives.exists())

    def test_label_goes_into_archive_name(self):
        result = self.rotate(label="2026-09")
        self.assertEqual(Path(result["archive"]).name, "apache_error.log.2026-09.2026-10-07T195312.gz")

    def test_two_rotations_never_overwrite_archives(self):
        first = self.rotate()
        self.log.write_bytes(b"second batch\n")
        second = self.rotate()
        self.assertNotEqual(first["archive"], second["archive"])
        with gzip.open(first["archive"], "rb") as f:
            self.assertEqual(f.read(), self.data)
        with gzip.open(second["archive"], "rb") as f:
            self.assertEqual(f.read(), b"second batch\n")

    def test_empty_log(self):
        self.log.write_bytes(b"")
        result = self.rotate()
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["bytes_archived"], 0)

    def test_accepts_explicit_path(self):
        result = self.rotate(log=str(self.log))
        self.assertEqual(result["status"], "success")


class FailedRotationTest(RotateTestCase):
    def fail_with(self, target, code):
        return mock.patch.object(target[0], target[1], side_effect=RotatorError(code, "simulated failure"))

    def test_compression_failure_does_not_truncate(self):
        with self.fail_with((rotate.compressor, "compress_log"), errors.COMPRESSION_FAILED):
            result = self.rotate()
        self.assertLogUnchanged(result)
        self.assertEqual(result["error_code"], errors.COMPRESSION_FAILED)
        self.assertEqual(result["steps"][-1], {**result["steps"][-1], "step": "compress", "ok": False})
        self.assertEqual(self.archive_files(), [], "snapshot must be cleaned up")

    def test_real_disk_error_during_compression_does_not_truncate(self):
        # `os` is one shared module: the 1st fsync is the snapshot's (let it pass),
        # the 2nd is the compressor's (simulate a full disk).
        real_fsync = os.fsync
        calls = []

        def fsync(fd):
            calls.append(fd)
            if len(calls) == 2:
                raise OSError(28, "No space left on device")
            return real_fsync(fd)

        with mock.patch.object(os, "fsync", side_effect=fsync):
            result = self.rotate()
        self.assertLogUnchanged(result)
        self.assertEqual(result["error_code"], errors.COMPRESSION_FAILED)
        self.assertEqual(self.archive_files(), [])

    def test_verification_failure_does_not_truncate_and_removes_archive(self):
        with self.fail_with((rotate.verifier, "verify_archive"), errors.VERIFY_FAILED):
            result = self.rotate()
        self.assertLogUnchanged(result)
        self.assertEqual(result["error_code"], errors.VERIFY_FAILED)
        self.assertEqual(result["archive_removed"], str(self.archives / ARCHIVE_NAME))
        self.assertEqual(self.archive_files(), [])

    def test_corrupted_archive_is_caught_before_truncation(self):
        real_compress = rotate.compressor.compress_log

        def compress_then_corrupt(*args, **kwargs):
            result = real_compress(*args, **kwargs)
            raw = bytearray(Path(result["archive"]).read_bytes())
            raw[len(raw) // 2] ^= 0xFF
            Path(result["archive"]).write_bytes(bytes(raw))
            return result

        with mock.patch.object(rotate.compressor, "compress_log", side_effect=compress_then_corrupt):
            result = self.rotate()
        self.assertLogUnchanged(result)
        self.assertEqual(result["error_code"], errors.VERIFY_FAILED)
        self.assertEqual(self.archive_files(), [])

    def test_snapshot_failure_does_not_truncate(self):
        with self.fail_with((rotate.snapshot, "snapshot_log"), errors.SNAPSHOT_FAILED):
            result = self.rotate()
        self.assertLogUnchanged(result)

    def test_truncate_failure_keeps_verified_archive(self):
        with self.fail_with((rotate.truncator, "truncate_log"), errors.TRUNCATE_FAILED):
            result = self.rotate()
        self.assertLogUnchanged(result)
        self.assertEqual(self.archive_files(), [ARCHIVE_NAME], "the verified archive is still valid")

    def test_missing_log_returns_structured_error(self):
        result = self.rotate(log="does_not_exist.log")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_code"], errors.LOG_NOT_FOUND)
        self.assertEqual(result["steps"][0]["step"], "identify")

    def test_path_outside_log_dir_is_refused(self):
        result = self.rotate(log="/etc/passwd")
        self.assertEqual(result["error_code"], errors.OUTSIDE_ALLOWED_DIR)
        self.assertFalse(self.archives.exists())

    @unittest.skipIf(os.geteuid() == 0, "root bypasses file permissions")
    def test_read_only_log_is_refused_before_archiving(self):
        os.chmod(self.log, 0o444)
        result = self.rotate()
        self.assertEqual(result["error_code"], errors.PERMISSION_DENIED)
        self.assertEqual(self.log.read_bytes(), self.data)
        self.assertEqual(self.archive_files(), [])

    def test_symlinked_log_is_refused(self):
        (self.logs / "link.log").symlink_to(self.log)
        result = self.rotate(log="link.log")
        self.assertEqual(result["error_code"], errors.SYMLINK_REJECTED)
        self.assertEqual(self.log.read_bytes(), self.data)


if __name__ == "__main__":
    unittest.main()
