"""Structured errors and results.

Every tool returns a plain dict so the master agent can consume it as JSON.
Failures are raised internally as RotatorError (with a stable error code)
and converted to a dict at the edge with .to_dict().
"""

# Stable error codes. The master agent can branch on these without parsing messages.
INVALID_REQUEST = "INVALID_REQUEST"
LOG_NOT_FOUND = "LOG_NOT_FOUND"
AMBIGUOUS_LOG = "AMBIGUOUS_LOG"
NOT_A_REGULAR_FILE = "NOT_A_REGULAR_FILE"
OUTSIDE_ALLOWED_DIR = "OUTSIDE_ALLOWED_DIR"
SYMLINK_REJECTED = "SYMLINK_REJECTED"
HARDLINK_REJECTED = "HARDLINK_REJECTED"
FILE_CHANGED = "FILE_CHANGED"
PERMISSION_DENIED = "PERMISSION_DENIED"
SNAPSHOT_FAILED = "SNAPSHOT_FAILED"
COMPRESSION_FAILED = "COMPRESSION_FAILED"
OS_ERROR = "OS_ERROR"


class RotatorError(Exception):
    """An expected, explainable failure of a tool."""

    def __init__(self, code: str, message: str, **details):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def to_dict(self, action: str = None) -> dict:
        result = {"status": "error", "error_code": self.code, "message": self.message}
        if action:
            result["action"] = action
        result.update(self.details)
        return result


def success(action: str, **fields) -> dict:
    """Build a successful structured result."""
    return {"status": "success", "action": action, **fields}


def from_os_error(err: OSError, path=None) -> RotatorError:
    """Translate an OSError (errno) into a RotatorError with a stable code."""
    import errno

    target = str(path or err.filename or "")
    if err.errno in (errno.EACCES, errno.EPERM):
        return RotatorError(PERMISSION_DENIED, f"Permission denied: {target}", path=target)
    if err.errno == errno.ENOENT:
        return RotatorError(LOG_NOT_FOUND, f"No such file: {target}", path=target)
    if err.errno == errno.ELOOP:
        return RotatorError(SYMLINK_REJECTED, f"Refusing to follow symlink: {target}", path=target)
    return RotatorError(OS_ERROR, f"{err.strerror or err}: {target}", path=target,
                        errno=err.errno)
