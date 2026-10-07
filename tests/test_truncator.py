"""Tests for in-place truncation (log_rotator/tools/truncator.py)."""

import os
import tempfile
import unittest
from pathlib import Path

from log_rotator import errors
from log_rotator.errors import RotatorError
from log_rotator.tools import truncator


class TruncatorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Path(self.tmp.name) / "apache_error.log"
        self.log.write_bytes(b"x" * 10000)
        self.fds = []

    def tearDown(self):
        for fd in self.fds:
            os.close(fd)
        self.tmp.cleanup()

    def open(self, flags=os.O_RDWR):
        fd = os.open(self.log, flags)
        self.fds.append(fd)
        return fd

    def test_truncates_to_zero_and_keeps_inode(self):
        inode = os.stat(self.log).st_ino
        result = truncator.truncate_log(self.open())
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["size_before"], 10000)
        self.assertEqual(result["size_after"], 0)
        self.assertEqual(result["bytes_removed"], 10000)
        self.assertEqual(result["inode_before"], inode)
        self.assertEqual(result["inode_after"], inode)
        self.assertTrue(result["inode_preserved"])
        self.assertEqual(os.stat(self.log).st_size, 0)
        self.assertEqual(os.stat(self.log).st_ino, inode)

    def test_file_still_exists_with_same_permissions(self):
        os.chmod(self.log, 0o640)
        truncator.truncate_log(self.open())
        self.assertTrue(self.log.exists())
        self.assertEqual(os.stat(self.log).st_mode & 0o777, 0o640)

    def test_other_append_descriptor_keeps_working(self):
        writer = self.open(os.O_WRONLY | os.O_APPEND)  # another "process" holding the log
        truncator.truncate_log(self.open())
        os.write(writer, b"after rotation\n")
        self.assertEqual(self.log.read_bytes(), b"after rotation\n")

    def test_partial_truncation(self):
        result = truncator.truncate_log(self.open(), length=100)
        self.assertEqual(result["size_after"], 100)
        self.assertEqual(os.stat(self.log).st_size, 100)

    def test_cannot_grow_file(self):
        with self.assertRaises(RotatorError) as ctx:
            truncator.truncate_log(self.open(), length=20000)
        self.assertEqual(ctx.exception.code, errors.INVALID_REQUEST)
        self.assertEqual(os.stat(self.log).st_size, 10000)

    def test_negative_length_rejected(self):
        with self.assertRaises(RotatorError):
            truncator.truncate_log(self.open(), length=-1)

    def test_read_only_descriptor_is_refused_by_kernel(self):
        with self.assertRaises(RotatorError) as ctx:
            truncator.truncate_log(self.open(os.O_RDONLY))
        self.assertEqual(ctx.exception.code, errors.TRUNCATE_FAILED)
        self.assertEqual(os.stat(self.log).st_size, 10000, "log must be untouched")

    def test_truncates_the_opened_inode_even_if_path_is_replaced(self):
        """ftruncate works on the descriptor, so a swapped path is never touched."""
        fd = self.open()
        os.rename(self.log, self.log.with_suffix(".old"))
        self.log.write_bytes(b"new file at the old name")
        truncator.truncate_log(fd)
        self.assertEqual(self.log.read_bytes(), b"new file at the old name")
        self.assertEqual(self.log.with_suffix(".old").stat().st_size, 0)

    def test_delete_and_recreate_changes_inode_for_comparison(self):
        """The WRONG way to rotate, shown for contrast: the inode changes."""
        writer = self.open(os.O_WRONLY | os.O_APPEND)
        inode = os.stat(self.log).st_ino
        os.unlink(self.log)
        self.log.write_bytes(b"")
        os.write(writer, b"this line is lost\n")
        self.assertNotEqual(os.stat(self.log).st_ino, inode)
        self.assertEqual(self.log.read_bytes(), b"", "writer's line went to the deleted inode")
        self.assertEqual(os.fstat(writer).st_nlink, 0)


if __name__ == "__main__":
    unittest.main()
