"""Snapshot: copy the current contents of an active log to a private file.

The writer keeps appending while we copy, so "the contents" must be pinned
down first: we fstat() the open descriptor and copy exactly the first
`length` bytes. Anything the writer appends after that point is not part
of this snapshot; it stays in the log and is handled by the caller.

OS concepts:

* pread(fd, n, offset) reads at an explicit position without moving the
  descriptor's file offset, so the copy never disturbs other users of fd.
* The snapshot is created with O_CREAT | O_EXCL, which fails if the name
  already exists, so an existing file is never overwritten, and with mode
  0600 because logs can contain sensitive data.
* fsync() forces the copied data from the page cache to the disk before we
  rely on it. Only then is it safe to think about truncating the original.
* A SHA-256 of the copied bytes lets later steps prove that the archive
  holds exactly these bytes.
"""

import hashlib
import os

from .. import config
from .. import errors
from ..errors import RotatorError


def snapshot_log(src_fd: int, dest_path, length: int = None, offset: int = 0) -> dict:
    """Copy bytes [offset, offset + length) of the open log `src_fd` to `dest_path`.

    `length` defaults to everything from `offset` to the current end of file.
    Returns the snapshot path, byte count and SHA-256 of the copied data.
    On any failure the partial snapshot is removed and SNAPSHOT_FAILED is raised;
    the source log is only ever read, never modified.
    """
    src_stat = os.fstat(src_fd)
    if length is None:
        length = max(src_stat.st_size - offset, 0)
    if length < 0 or offset < 0:
        raise RotatorError(errors.INVALID_REQUEST, "offset and length must not be negative",
                           offset=offset, length=length)

    dest_path = os.fspath(dest_path)
    try:
        dest_fd = os.open(dest_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError as err:
        raise RotatorError(errors.SNAPSHOT_FAILED, f"Cannot create snapshot {dest_path}: {err.strerror}",
                           snapshot=dest_path, errno=err.errno) from None

    digest = hashlib.sha256()
    copied = 0
    try:
        while copied < length:
            chunk = os.pread(src_fd, min(config.COPY_CHUNK_SIZE, length - copied), offset + copied)
            if not chunk:
                # The log got shorter while we were copying (someone else truncated it).
                raise RotatorError(errors.SNAPSHOT_FAILED,
                                   f"Log shrank during snapshot: expected {length} bytes, got {copied}",
                                   expected=length, copied=copied)
            _write_all(dest_fd, chunk)
            digest.update(chunk)
            copied += len(chunk)
        os.fsync(dest_fd)
    except BaseException as err:
        os.close(dest_fd)
        os.unlink(dest_path)
        if isinstance(err, OSError):
            raise RotatorError(errors.SNAPSHOT_FAILED, f"Snapshot failed: {err.strerror}",
                               snapshot=dest_path, errno=err.errno) from None
        raise
    os.close(dest_fd)

    return errors.success(
        "snapshot_log",
        snapshot=dest_path,
        source_inode=src_stat.st_ino,
        offset=offset,
        bytes=copied,
        sha256=digest.hexdigest(),
    )


def _write_all(fd: int, data: bytes) -> None:
    """write() may write fewer bytes than asked (a short write); loop until done."""
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]
