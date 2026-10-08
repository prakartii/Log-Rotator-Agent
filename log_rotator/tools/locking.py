"""File locking with flock(): one rotation at a time, and cooperative writers.

Two different locks are used, for two different problems:

1. The ROTATION lock - two rotators must never work on the same log at the
   same time (both would archive the same bytes and the second ftruncate()
   could cut off lines the first one already counted as safe). Each rotator
   takes an EXCLUSIVE flock on a small lock file next to the log:

       logs/apache_error.log  ->  logs/.apache_error.log.rotate.lock

   The lock file is never deleted. If it were, a second rotator could create
   a NEW lock file (new inode) and lock that one, while the first still holds
   the lock on the old, deleted inode - both would think they own the lock.

2. The WRITER lock - a cooperative writer takes a SHARED flock on the log
   itself around every write(). Shared locks do not block each other, so
   several writers can still append at once. Just before ftruncate() the
   rotator takes an EXCLUSIVE flock on the log: the kernel makes it wait
   until in-flight writes finish and then holds new writes back until the
   log has been truncated. Nothing can land between the final catch-up and
   ftruncate(), so no line is lost.

flock() locks are ADVISORY: they only work between processes that use them.
A writer that never calls flock() is not blocked (that is why the catch-up
step still exists). Locks belong to the open file description, so the kernel
releases them automatically when the process closes the descriptor or dies -
a crashed rotator can never leave a log locked forever.
"""

import fcntl
import os
import time
from contextlib import contextmanager
from pathlib import Path

from .. import config, errors
from ..errors import RotatorError


def lock_path_for(log_path) -> Path:
    """The rotation lock file for `log_path`: a hidden file in the same directory."""
    log_path = Path(os.fspath(log_path))
    return log_path.with_name(f".{log_path.name}.rotate.lock")


def acquire(fd: int, operation: int, timeout: float, poll: float = 0.02):
    """flock(fd, operation) without blocking forever.

    LOCK_NB makes flock() fail with EWOULDBLOCK instead of sleeping inside
    the kernel, so we can retry until `timeout` seconds have passed.
    Returns the milliseconds spent waiting, or None if the lock stayed busy.
    """
    start = time.monotonic()
    while True:
        try:
            fcntl.flock(fd, operation | fcntl.LOCK_NB)
            return round((time.monotonic() - start) * 1000, 2)
        except BlockingIOError:
            if time.monotonic() - start >= timeout:
                return None
            time.sleep(poll)


def _write_holder(fd: int) -> None:
    """Write our pid into the lock file, so a refused rotator can say who holds it."""
    os.ftruncate(fd, 0)
    os.pwrite(fd, f"{os.getpid()}\n".encode(), 0)


def _read_holder(fd: int):
    """The pid stored by the current holder, or None if it is missing or unreadable."""
    try:
        return int(os.pread(fd, 32, 0).decode().strip())
    except (OSError, ValueError):
        return None


@contextmanager
def rotation_lock(log_path, timeout: float = None):
    """Hold the exclusive rotation lock of `log_path` for the duration of a `with` block.

    Raises ROTATION_IN_PROGRESS if another rotator keeps it for `timeout`
    seconds (default: config.ROTATION_LOCK_TIMEOUT). Yields a dict with the
    lock file and how long we waited.
    """
    lock_path = lock_path_for(log_path)
    timeout = config.ROTATION_LOCK_TIMEOUT if timeout is None else timeout
    try:
        # O_NOFOLLOW: a symlink planted at the lock path must not redirect us.
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as err:
        raise errors.from_os_error(err, lock_path) from None
    try:
        waited = acquire(fd, fcntl.LOCK_EX, timeout)
        if waited is None:
            raise RotatorError(errors.ROTATION_IN_PROGRESS,
                               f"Another rotation of {log_path} is already running",
                               lock_file=str(lock_path), locked_by_pid=_read_holder(fd))
        _write_holder(fd)
        yield {"lock_file": str(lock_path), "waited_ms": waited}
    finally:
        os.close(fd)  # closing the descriptor releases the flock


@contextmanager
def writer_lock(log_fd: int, timeout: float = None):
    """Hold back cooperative writers (exclusive flock on the log) inside a `with` block.

    Waits up to `timeout` seconds (default: config.WRITER_LOCK_TIMEOUT) for
    writes in progress to finish. If a writer keeps its shared lock longer,
    this does NOT fail: rotation continues unprotected and `acquired` is False.
    Yields {"acquired": bool, "waited_ms": float | None}.
    """
    timeout = config.WRITER_LOCK_TIMEOUT if timeout is None else timeout
    waited = acquire(log_fd, fcntl.LOCK_EX, timeout)
    try:
        yield {"acquired": waited is not None, "waited_ms": waited}
    finally:
        if waited is not None:
            # Unlock explicitly: the rotator keeps using log_fd after this block.
            fcntl.flock(log_fd, fcntl.LOCK_UN)
