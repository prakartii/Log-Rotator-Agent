"""Tests for archive verification (log_rotator/tools/verifier.py).

Verification is the gate before truncation, so most tests here check that
every kind of bad archive is REJECTED.
"""

import gzip
import hashlib
import os
import tempfile
import unittest
from pathlib import Path

from log_rotator import errors
from log_rotator.errors import RotatorError
from log_rotator.tools import compressor, verifier


class VerifierTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.data = b"".join(f"[error] worker {i} crashed\n".encode() for i in range(10000))
        self.sha = hashlib.sha256(self.data).hexdigest()
        snap = self.base / "snap"
        snap.write_bytes(self.data)
        result = compressor.compress_log(snap, archive_dir=self.base / "rotated", name="app.log.gz")
        self.archive = Path(result["archive"])

    def tearDown(self):
        self.tmp.cleanup()

    def verify(self, archive=None, sha=None, size=None):
        return verifier.verify_archive(archive or self.archive,
                                       self.sha if sha is None else sha,
                                       len(self.data) if size is None else size)

    def assertVerifyFails(self, **kwargs):
        with self.assertRaises(RotatorError) as ctx:
            self.verify(**kwargs)
        self.assertEqual(ctx.exception.code, errors.VERIFY_FAILED)
        return ctx.exception

    def corrupt_byte(self, position):
        raw = bytearray(self.archive.read_bytes())
        raw[position] ^= 0xFF
        self.archive.write_bytes(bytes(raw))

    # --- good archive -----------------------------------------------------

    def test_valid_archive_passes_all_checks(self):
        result = self.verify()
        self.assertEqual(result["status"], "success")
        self.assertTrue(result["verified"])
        self.assertEqual(result["uncompressed_size"], len(self.data))
        self.assertEqual(result["sha256"], self.sha)
        self.assertEqual(result["archive_size"], self.archive.stat().st_size)
        self.assertEqual(result["checks"], {"regular_file": True, "gzip_valid": True,
                                            "size_match": True, "sha256_match": True})

    def test_empty_log_archive(self):
        empty = self.base / "empty.gz"
        with gzip.open(empty, "wb"):
            pass
        result = verifier.verify_archive(empty, hashlib.sha256(b"").hexdigest(), 0)
        self.assertTrue(result["verified"])

    def test_multi_member_gzip_is_read_completely(self):
        # gzip allows several compressed members back to back; all must be checked.
        multi = self.base / "multi.gz"
        multi.write_bytes(gzip.compress(self.data[:1000]) + gzip.compress(self.data[1000:]))
        self.assertTrue(verifier.verify_archive(multi, self.sha, len(self.data))["verified"])

    # --- damaged archives -------------------------------------------------

    def test_corrupted_data_fails_crc(self):
        self.corrupt_byte(self.archive.stat().st_size // 2)
        err = self.assertVerifyFails()
        self.assertFalse(err.details["checks"]["gzip_valid"])

    def test_corrupted_crc_trailer_fails(self):
        self.corrupt_byte(-6)  # inside the CRC-32 field of the 8-byte gzip trailer
        self.assertVerifyFails()

    def test_truncated_archive_fails(self):
        raw = self.archive.read_bytes()
        self.archive.write_bytes(raw[: len(raw) // 2])
        err = self.assertVerifyFails()
        self.assertFalse(err.details["checks"]["gzip_valid"])

    def test_zero_byte_archive_fails(self):
        self.archive.write_bytes(b"")
        self.assertVerifyFails(sha=self.sha, size=len(self.data))

    def test_not_a_gzip_file_fails(self):
        self.archive.write_bytes(self.data)  # plain text with a .gz name
        self.assertVerifyFails()

    # --- wrong content ----------------------------------------------------

    def test_size_mismatch_fails(self):
        err = self.assertVerifyFails(size=len(self.data) + 1)
        self.assertFalse(err.details["checks"]["size_match"])
        self.assertEqual(err.details["actual_size"], len(self.data))

    def test_checksum_mismatch_fails(self):
        err = self.assertVerifyFails(sha=hashlib.sha256(b"other").hexdigest())
        self.assertTrue(err.details["checks"]["size_match"])
        self.assertFalse(err.details["checks"]["sha256_match"])
        self.assertEqual(err.details["actual_sha256"], self.sha)

    def test_same_size_different_content_fails(self):
        other = bytes(reversed(self.data))
        self.archive.write_bytes(gzip.compress(other))
        err = self.assertVerifyFails()
        self.assertFalse(err.details["checks"]["sha256_match"])

    # --- wrong file -------------------------------------------------------

    def test_missing_archive_fails(self):
        self.archive.unlink()
        self.assertVerifyFails()

    def test_symlink_archive_fails(self):
        link = self.base / "link.gz"
        link.symlink_to(self.archive)
        self.assertVerifyFails(archive=link)

    def test_directory_fails(self):
        err = self.assertVerifyFails(archive=self.base)
        self.assertNotIn("sha256_match", err.details["checks"])


if __name__ == "__main__":
    unittest.main()
