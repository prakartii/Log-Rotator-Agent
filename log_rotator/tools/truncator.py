"""Truncation: empty the active log IN PLACE.

Why ftruncate() and not "delete and recreate"?

A file name is just a directory entry pointing at an inode. The writer
process does not hold the name; it holds a file descriptor that points
(through the kernel's open-file table) at the INODE.

* rm + create:   the name now points at a NEW inode. The writer still
                 writes to the OLD inode, which has no name any more:
                 its new log lines are invisible and silently lost, and
                 the disk space is not freed until the writer restarts.
* ftruncate(fd, 0): the SAME inode is cut down to 0 bytes. The name, the
                 inode number, the permissions and every open descriptor
                 stay valid. A writer using O_APPEND simply continues at
                 the new end of file (offset 0).

We call ftruncate() on the descriptor we validated and opened earlier
(not truncate() on the path), so we truncate exactly the file we
archived even if someone renames or replaces the path in the meantime.
The descriptor must be open for writing, otherwise the kernel refuses.
"""

import os

from .. import errors
from ..errors import RotatorError


def truncate_log(fd: int, length: int = 0) -> dict:
    """Shrink the open log `fd` to `length` bytes and report before/after state."""
    before = os.fstat(fd)
    if length < 0 or length > before.st_size:
        raise RotatorError(errors.INVALID_REQUEST,
                           f"Truncation length must be between 0 and {before.st_size}",
                           length=length, size=before.st_size)
    try:
        os.ftruncate(fd, length)
    except OSError as err:
        # EINVAL/EBADF: descriptor not open for writing; EPERM: e.g. an append-only (chattr +a) file.
        raise RotatorError(errors.TRUNCATE_FAILED, f"ftruncate failed: {err.strerror}",
                           errno=err.errno, inode=before.st_ino) from None
    after = os.fstat(fd)

    return errors.success(
        "truncate_log",
        inode_before=before.st_ino,
        inode_after=after.st_ino,
        inode_preserved=(before.st_dev, before.st_ino) == (after.st_dev, after.st_ino),
        size_before=before.st_size,
        size_after=after.st_size,
        bytes_removed=before.st_size - after.st_size,
    )
