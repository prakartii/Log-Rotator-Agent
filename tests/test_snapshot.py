"""Tests for the snapshot tool (log_rotator/tools/snapshot.py)."""

import hashlib
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from log_rotator import config, errors
from log_rotator.errors import RotatorError
from log_rotator.tools import snapshot


class SnapshotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.log = base / "apache_error.log"
        self.data = b"".join(f"line {i}\n".encode() for i in range(5000))
        self.log.write_bytes(self.data)
        self.dest = base / "snap"
        self.fd = os.open(self.log, os.O_RDONLY)

    def tearDown(self):
        os.close(self.fd)
        self.tmp.cleanup()

    def test_copies_whole_log_with_checksum(self):
        result = snapshot.snapshot_log(self.fd, self.dest)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["bytes"], len(self.data))
        self.assertEqual(result["sha256"], hashlib.sha256(self.data).hexdigest())
        self.assertEqual(result["source_inode"], os.stat(self.log).st_ino)
        self.assertEqual(self.dest.read_bytes(), self.data)

    def test_snapshot_is_private(self):
        snapshot.snapshot_log(self.fd, self.dest)
        self.assertEqual(stat.S_IMODE(os.stat(self.dest).st_mode), 0o600)

    def test_copies_exact_length_even_if_writer_appends_later(self):
        length = os.fstat(self.fd).st_size
        with open(self.log, "ab") as writer:
            writer.write(b"appended after the size was pinned\n")
        result = snapshot.snapshot_log(self.fd, self.dest, length=length)
        self.assertEqual(result["bytes"], length)
        self.assertEqual(self.dest.read_bytes(), self.data)

    def test_offset_copies_only_the_tail(self):
        result = snapshot.snapshot_log(self.fd, self.dest, offset=100)
        self.assertEqual(self.dest.read_bytes(), self.data[100:])
        self.assertEqual(result["offset"], 100)

    def test_does_not_move_descriptor_offset(self):
        os.lseek(self.fd, 42, os.SEEK_SET)
        snapshot.snapshot_log(self.fd, self.dest)
        self.assertEqual(os.lseek(self.fd, 0, os.SEEK_CUR), 42)

    def test_source_log_is_unchanged(self):
        inode = os.stat(self.log).st_ino
        snapshot.snapshot_log(self.fd, self.dest)
        self.assertEqual(self.log.read_bytes(), self.data)
        self.assertEqual(os.stat(self.log).st_ino, inode)

    def test_copies_in_chunks_larger_than_chunk_size(self):
        with mock.patch.object(config, "COPY_CHUNK_SIZE", 1000):
            result = snapshot.snapshot_log(self.fd, self.dest)
        self.assertEqual(result["bytes"], len(self.data))
        self.assertEqual(self.dest.read_bytes(), self.data)

    def test_empty_log(self):
        self.log.write_bytes(b"")
        result = snapshot.snapshot_log(self.fd, self.dest)
        self.assertEqual(result["bytes"], 0)
        self.assertEqual(self.dest.read_bytes(), b"")

    def test_never_overwrites_existing_file(self):
        self.dest.write_bytes(b"precious")
        with self.assertRaises(RotatorError) as ctx:
            snapshot.snapshot_log(self.fd, self.dest)
        self.assertEqual(ctx.exception.code, errors.SNAPSHOT_FAILED)
        self.assertEqual(self.dest.read_bytes(), b"precious")

    def test_log_shrinking_during_copy_fails_and_cleans_up(self):
        length = len(self.data)
        os.truncate(self.log, 100)  # someone else truncated the log
        with self.assertRaises(RotatorError) as ctx:
            snapshot.snapshot_log(self.fd, self.dest, length=length)
        self.assertEqual(ctx.exception.code, errors.SNAPSHOT_FAILED)
        self.assertEqual(ctx.exception.details["copied"], 100)
        self.assertFalse(self.dest.exists(), "partial snapshot must be removed")

    def test_write_error_cleans_up(self):
        with mock.patch.object(snapshot.os, "fsync", side_effect=OSError(5, "Input/output error")):
            with self.assertRaises(RotatorError) as ctx:
                snapshot.snapshot_log(self.fd, self.dest)
        self.assertEqual(ctx.exception.code, errors.SNAPSHOT_FAILED)
        self.assertFalse(self.dest.exists())
        self.assertEqual(self.log.read_bytes(), self.data)

    def test_handles_short_writes(self):
        real_write = os.write
        with mock.patch.object(snapshot.os, "write", side_effect=lambda fd, b: real_write(fd, b[:7])):
            snapshot.snapshot_log(self.fd, self.dest)
        self.assertEqual(self.dest.read_bytes(), self.data)

    def test_negative_length_rejected(self):
        with self.assertRaises(RotatorError) as ctx:
            snapshot.snapshot_log(self.fd, self.dest, length=-1)
        self.assertEqual(ctx.exception.code, errors.INVALID_REQUEST)


if __name__ == "__main__":
    unittest.main()
