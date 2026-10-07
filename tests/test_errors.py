import errno
import unittest

from log_rotator import errors
from log_rotator.errors import RotatorError


class ErrorsTest(unittest.TestCase):
    def test_error_to_dict(self):
        err = RotatorError(errors.LOG_NOT_FOUND, "No such log: x.log", path="x.log")
        self.assertEqual(err.to_dict(action="rotate"), {
            "status": "error",
            "error_code": "LOG_NOT_FOUND",
            "message": "No such log: x.log",
            "action": "rotate",
            "path": "x.log",
        })

    def test_success(self):
        self.assertEqual(errors.success("get_log_info", size=10),
                         {"status": "success", "action": "get_log_info", "size": 10})

    def test_os_error_translation(self):
        cases = {
            errno.EACCES: errors.PERMISSION_DENIED,
            errno.EPERM: errors.PERMISSION_DENIED,
            errno.ENOENT: errors.LOG_NOT_FOUND,
            errno.ELOOP: errors.SYMLINK_REJECTED,
            errno.EIO: errors.OS_ERROR,
        }
        for code, expected in cases.items():
            err = errors.from_os_error(OSError(code, "boom", "/tmp/f"))
            self.assertEqual(err.code, expected)
            self.assertEqual(err.details["path"], "/tmp/f")


if __name__ == "__main__":
    unittest.main()
