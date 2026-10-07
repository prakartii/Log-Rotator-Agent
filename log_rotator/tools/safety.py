"""Path validation: decide whether the agent is allowed to touch a file.

Rotation ends with truncate(), which destroys data, so before any tool
touches a file we check:

1. The path resolves (after following symlinks in its *directories* and
   removing "..") to a location inside an allowed root such as logs/.
   This blocks requests like "../../etc/passwd".
2. The file itself is not a symbolic link (checked with lstat(), which
   does NOT follow links). Otherwise logs/evil.log -> /etc/shadow would
   pass the directory check while pointing somewhere else.
3. It is a regular file (S_ISREG), not a directory, FIFO, socket or device.
4. It has exactly one hard link (st_nlink == 1). A hard link is a second
   name for the SAME inode, so a hard link inside logs/ to a file outside
   it would otherwise slip past the directory check.
5. The current process has the needed permissions (access()).

Between checking a path and opening it, another process could swap the
file (a "TOCTOU" race: time-of-check to time-of-use). open_validated()
closes that gap: it opens with O_NOFOLLOW and then compares the
(device, inode) pair of the opened descriptor, via fstat(), with the
pair that was validated.
"""

import os
import stat
from pathlib import Path

from .. import config
from .. import errors
from ..errors import RotatorError


def _resolved_roots(allowed_roots=None):
    roots = config.ALLOWED_LOG_ROOTS if allowed_roots is None else allowed_roots
    return [Path(os.path.realpath(r)) for r in roots]


def is_within_allowed_roots(path, allowed_roots=None) -> bool:
    real = Path(os.path.realpath(path))
    return any(real == root or real.is_relative_to(root) for root in _resolved_roots(allowed_roots))


def validate_log_path(path, allowed_roots=None, need_write=True) -> dict:
    """Validate `path` as a log the agent may operate on.

    Returns a dict with the absolute path and the lstat() result's
    (device, inode) so the caller can later confirm it opened the same file.
    Raises RotatorError otherwise.
    """
    if path is None or str(path).strip() == "" or "\0" in str(path):
        raise RotatorError(errors.INVALID_REQUEST, "A log path is required", path=str(path))

    path = Path(os.path.abspath(os.path.expanduser(str(path))))

    # Resolve symlinks in the parent directories only; the last component is checked
    # separately with lstat() so a symlinked log file is rejected rather than followed.
    real_path = Path(os.path.realpath(path.parent)) / path.name
    if not is_within_allowed_roots(real_path.parent, allowed_roots) or path.name in ("", ".", ".."):
        raise RotatorError(
            errors.OUTSIDE_ALLOWED_DIR,
            f"{path} is outside the allowed log directories",
            path=str(path), allowed_roots=[str(r) for r in _resolved_roots(allowed_roots)],
        )

    try:
        st = os.lstat(real_path)  # lstat: do NOT follow a symlink
    except OSError as err:
        raise errors.from_os_error(err, path) from None

    if stat.S_ISLNK(st.st_mode):
        raise RotatorError(errors.SYMLINK_REJECTED,
                           f"{path} is a symbolic link; refusing to follow it", path=str(path))
    if not stat.S_ISREG(st.st_mode):
        raise RotatorError(errors.NOT_A_REGULAR_FILE,
                           f"{path} is not a regular file ({stat.filemode(st.st_mode)})",
                           path=str(path))
    if st.st_nlink > 1:
        raise RotatorError(errors.HARDLINK_REJECTED,
                           f"{path} has {st.st_nlink} hard links; refusing to modify a shared inode",
                           path=str(path), nlink=st.st_nlink)

    check_access(real_path, need_write=need_write)
    return {"path": str(real_path), "device": st.st_dev, "inode": st.st_ino}


def check_access(path, need_write=True) -> None:
    """Permission check with access(): can *this* process read (and write) the file?"""
    mode = os.R_OK | (os.W_OK if need_write else 0)
    if not os.access(path, mode):
        st = os.stat(path)
        needed = "read/write" if need_write else "read"
        raise RotatorError(
            errors.PERMISSION_DENIED,
            f"No {needed} permission on {path} (mode {stat.filemode(st.st_mode)}, owner uid {st.st_uid})",
            path=str(path), mode=stat.filemode(st.st_mode), owner_uid=st.st_uid,
        )


def open_validated(path, flags=os.O_RDONLY, allowed_roots=None) -> tuple:
    """Validate `path`, open it safely and return (fd, validated_info).

    O_NOFOLLOW makes open() fail with ELOOP if the last component became a
    symlink after validation. O_NONBLOCK makes sure we never hang if the
    file was swapped for a FIFO; it has no effect on regular files.
    The caller owns the returned descriptor and must os.close() it.
    """
    need_write = (flags & os.O_ACCMODE) != os.O_RDONLY
    info = validate_log_path(path, allowed_roots=allowed_roots, need_write=need_write)
    try:
        fd = os.open(info["path"], flags | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as err:
        raise errors.from_os_error(err, info["path"]) from None

    st = os.fstat(fd)
    if (st.st_dev, st.st_ino) != (info["device"], info["inode"]) or not stat.S_ISREG(st.st_mode):
        os.close(fd)
        raise RotatorError(errors.FILE_CHANGED,
                           f"{info['path']} changed between validation and open",
                           path=info["path"])
    return fd, info
