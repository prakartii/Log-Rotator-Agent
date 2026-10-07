"""Tests for the gzip compressor (log_rotator/tools/compressor.py)."""

import gzip
import hashlib
import os
import stat
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from log_rotator import errors
from log_rotator.errors import RotatorError
from log_rotator.tools import compressor


class CompressorTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.snapshot = base / "snap"
        self.data = b"".join(f"[error] request {i} failed\n".encode() for i in range(20000))
        self.snapshot.write_bytes(self.data)
        self.archives = base / "rotated_logs"

    def tearDown(self):
        if self.archives.exists():
            os.chmod(self.archives, 0o750)
        self.tmp.cleanup()

    def compress(self, **kwargs):
        kwargs.setdefault("name", "apache_error.log.2026-10-07T120000.gz")
        kwargs.setdefault("original_name", "apache_error.log")
        return compressor.compress_log(self.snapshot, archive_dir=self.archives, **kwargs)

    def archive_files(self):
        return sorted(p.name for p in self.archives.iterdir())


class ArchiveNameTest(unittest.TestCase):
    when = datetime(2026, 10, 7, 19, 53, 12)

    def test_default_name(self):
        self.assertEqual(compressor.archive_name("logs/apache_error.log", when=self.when),
                         "apache_error.log.2026-10-07T195312.gz")

    def test_name_with_label(self):
        self.assertEqual(compressor.archive_name("/x/logs/apache_error.log", when=self.when, label="2026-09"),
                         "apache_error.log.2026-09.2026-10-07T195312.gz")

    def test_unsafe_label_rejected(self):
        for label in ("../etc", "a b", "x/y", ""):
            with self.assertRaises(RotatorError) as ctx:
                compressor.archive_name("app.log", when=self.when, label=label)
            self.assertEqual(ctx.exception.code, errors.INVALID_REQUEST)


class CompressLogTest(CompressorTestCase):
    def test_archive_round_trips_to_original_bytes(self):
        result = self.compress()
        self.assertEqual(result["status"], "success")
        with gzip.open(result["archive"], "rb") as f:
            self.assertEqual(f.read(), self.data)

    def test_result_fields(self):
        result = self.compress()
        archive = Path(result["archive"])
        self.assertEqual(archive, self.archives / "apache_error.log.2026-10-07T120000.gz")
        self.assertEqual(result["archive_size"], archive.stat().st_size)
        self.assertEqual(result["original_size"], len(self.data))
        self.assertEqual(result["sha256"], hashlib.sha256(self.data).hexdigest())
        self.assertLess(result["archive_size"], len(self.data))
        self.assertAlmostEqual(result["compression_ratio"], result["archive_size"] / len(self.data), places=3)

    def test_gzip_header_stores_original_name(self):
        archive = Path(self.compress()["archive"]).read_bytes()
        flags = archive[3]
        self.assertTrue(flags & 0x08, "FNAME flag should be set")
        self.assertEqual(archive[10:10 + len(b"apache_error.log") + 1], b"apache_error.log\0")

    def test_creates_archive_dir_and_sets_permissions(self):
        result = self.compress()
        self.assertTrue(self.archives.is_dir())
        mode = stat.S_IMODE(os.stat(result["archive"]).st_mode)
        self.assertEqual(mode & 0o027, 0, f"archive must not be group-writable or world-accessible: {oct(mode)}")

    def test_no_temp_files_left_behind(self):
        self.compress()
        self.assertEqual(self.archive_files(), ["apache_error.log.2026-10-07T120000.gz"])

    def test_source_snapshot_is_untouched(self):
        self.compress()
        self.assertEqual(self.snapshot.read_bytes(), self.data)

    def test_existing_archive_is_never_overwritten(self):
        self.archives.mkdir()
        existing = self.archives / "apache_error.log.2026-10-07T120000.gz"
        existing.write_bytes(b"older archive")
        result = self.compress()
        self.assertEqual(existing.read_bytes(), b"older archive")
        self.assertEqual(Path(result["archive"]).name, "apache_error.log.2026-10-07T120000-1.gz")
        result2 = self.compress()
        self.assertEqual(Path(result2["archive"]).name, "apache_error.log.2026-10-07T120000-2.gz")

    def test_works_without_hard_link_support(self):
        with mock.patch.object(compressor.os, "link", side_effect=PermissionError(1, "Operation not permitted")):
            result = self.compress()
        with gzip.open(result["archive"], "rb") as f:
            self.assertEqual(f.read(), self.data)
        self.assertEqual(len(self.archive_files()), 1)

    def test_empty_snapshot(self):
        self.snapshot.write_bytes(b"")
        result = self.compress()
        self.assertEqual(result["original_size"], 0)
        self.assertEqual(result["compression_ratio"], 0.0)
        with gzip.open(result["archive"], "rb") as f:
            self.assertEqual(f.read(), b"")

    def test_default_name_uses_original_name(self):
        result = compressor.compress_log(self.snapshot, archive_dir=self.archives, original_name="app.log")
        self.assertRegex(Path(result["archive"]).name, r"^app\.log\.\d{4}-\d\d-\d\dT\d{6}\.gz$")

    def test_invalid_name_rejected(self):
        for name in ("../escape.gz", ".hidden.gz"):
            with self.assertRaises(RotatorError) as ctx:
                self.compress(name=name)
            self.assertEqual(ctx.exception.code, errors.INVALID_REQUEST)


class AppendGzipMemberTest(CompressorTestCase):
    def setUp(self):
        super().setUp()
        self.archive = Path(self.compress()["archive"])
        self.verified_bytes = self.archive.read_bytes()

    def test_appended_data_follows_original(self):
        result = compressor.append_gzip_member(self.archive, b"late line 1\nlate line 2\n")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["bytes_added"], 24)
        self.assertEqual(result["archive_size"], self.archive.stat().st_size)
        with gzip.open(self.archive, "rb") as f:
            self.assertEqual(f.read(), self.data + b"late line 1\nlate line 2\n")

    def test_original_member_bytes_are_untouched(self):
        compressor.append_gzip_member(self.archive, b"more\n")
        self.assertTrue(self.archive.read_bytes().startswith(self.verified_bytes))

    def test_several_appends(self):
        for i in range(3):
            compressor.append_gzip_member(self.archive, f"extra {i}\n".encode())
        with gzip.open(self.archive, "rb") as f:
            self.assertEqual(f.read(), self.data + b"extra 0\nextra 1\nextra 2\n")

    def test_failed_write_rolls_back_to_verified_archive(self):
        real_write = os.write

        def write_half_then_fail(fd, data):
            real_write(fd, bytes(data[: len(data) // 2]))
            raise OSError(28, "No space left on device")

        with mock.patch.object(compressor.os, "write", side_effect=write_half_then_fail):
            with self.assertRaises(RotatorError) as ctx:
                compressor.append_gzip_member(self.archive, b"x" * 5000)
        self.assertEqual(ctx.exception.code, errors.COMPRESSION_FAILED)
        self.assertEqual(self.archive.read_bytes(), self.verified_bytes, "archive must be rolled back")

    def test_missing_archive(self):
        self.archive.unlink()
        with self.assertRaises(RotatorError) as ctx:
            compressor.append_gzip_member(self.archive, b"x")
        self.assertEqual(ctx.exception.code, errors.COMPRESSION_FAILED)

    def test_refuses_symlink(self):
        link = self.archives / "link.gz"
        link.symlink_to(self.archive)
        with self.assertRaises(RotatorError):
            compressor.append_gzip_member(link, b"x")
        self.assertEqual(self.archive.read_bytes(), self.verified_bytes)


class CompressionFailureTest(CompressorTestCase):
    def test_missing_source(self):
        self.snapshot.unlink()
        with self.assertRaises(RotatorError) as ctx:
            self.compress()
        self.assertEqual(ctx.exception.code, errors.COMPRESSION_FAILED)

    def test_disk_error_during_write_removes_partial_archive(self):
        with mock.patch.object(compressor.os, "fsync", side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(RotatorError) as ctx:
                self.compress()
        self.assertEqual(ctx.exception.code, errors.COMPRESSION_FAILED)
        self.assertIn("No space left on device", ctx.exception.message)
        self.assertEqual(self.archive_files(), [], "no partial or temporary archive may remain")
        self.assertEqual(self.snapshot.read_bytes(), self.data)

    def test_gzip_error_removes_partial_archive(self):
        with mock.patch.object(compressor.gzip.GzipFile, "write", side_effect=ValueError("zlib exploded")):
            with self.assertRaises(RotatorError) as ctx:
                self.compress()
        self.assertEqual(ctx.exception.code, errors.COMPRESSION_FAILED)
        self.assertEqual(self.archive_files(), [])

    def test_archive_dir_is_a_file(self):
        self.archives.write_text("not a directory")
        with self.assertRaises(RotatorError):
            self.compress()

    def test_archive_dir_symlink_rejected(self):
        real = Path(self.tmp.name) / "elsewhere"
        real.mkdir()
        self.archives.symlink_to(real, target_is_directory=True)
        with self.assertRaises(RotatorError) as ctx:
            self.compress()
        self.assertEqual(ctx.exception.code, errors.NOT_A_REGULAR_FILE)
        self.assertEqual(list(real.iterdir()), [])

    @unittest.skipIf(os.geteuid() == 0, "root bypasses file permissions")
    def test_read_only_archive_dir(self):
        self.archives.mkdir()
        os.chmod(self.archives, 0o500)
        with self.assertRaises(RotatorError) as ctx:
            self.compress()
        self.assertEqual(ctx.exception.code, errors.PERMISSION_DENIED)


if __name__ == "__main__":
    unittest.main()
