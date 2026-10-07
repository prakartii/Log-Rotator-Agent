"""Verification: prove that an archive really contains the snapshot.

Truncating the active log destroys its contents, so it is only allowed
after the archive has been read back FROM DISK and shown to hold exactly
the bytes that were snapshotted. Checks, in order:

1. The archive is a regular file (lstat/fstat; no symlink, opened with O_NOFOLLOW).
2. It is a valid gzip stream. Python's gzip module checks every member's
   CRC-32 and stored length (ISIZE) while decompressing, and raises an
   error for corrupt or truncated data.
3. The decompressed size equals the snapshot size.
4. The SHA-256 of the decompressed data equals the snapshot's SHA-256.
   CRC-32 only detects accidental damage; SHA-256 also proves that this is
   the right content and not, for example, an older archive with the same name.

Only when every check passes is `verified` true.
"""

import gzip
import hashlib
import os
import stat
import zlib

from .. import config
from .. import errors
from ..errors import RotatorError


def verify_archive(archive_path, expected_sha256: str, expected_size: int) -> dict:
    """Decompress `archive_path` and compare it with the snapshot's size and checksum.

    Returns a success result with the individual checks, or raises
    VERIFY_FAILED with the check that failed and the expected/actual values.
    """
    archive_path = os.fspath(archive_path)
    checks = {}

    def fail(message, **details):
        raise RotatorError(errors.VERIFY_FAILED, message, archive=archive_path, checks=checks, **details)

    try:
        fd = os.open(archive_path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as err:
        fail(f"Cannot open archive: {err.strerror}", errno=err.errno)

    digest = hashlib.sha256()
    size = 0
    try:
        st = os.fstat(fd)
        checks["regular_file"] = stat.S_ISREG(st.st_mode)
        if not checks["regular_file"]:
            fail("Archive is not a regular file")

        with os.fdopen(fd, "rb", closefd=False) as raw, gzip.GzipFile(fileobj=raw, mode="rb") as gz:
            try:
                while True:
                    chunk = gz.read(config.COPY_CHUNK_SIZE)
                    if not chunk:
                        break
                    digest.update(chunk)
                    size += len(chunk)
            except (OSError, EOFError, zlib.error) as err:
                # BadGzipFile (an OSError) covers bad headers and CRC/length mismatches;
                # EOFError means the archive was cut off before its end marker.
                checks["gzip_valid"] = False
                fail(f"Archive is not a valid gzip file: {err}")
        checks["gzip_valid"] = True
    finally:
        os.close(fd)

    checks["size_match"] = size == expected_size
    if not checks["size_match"]:
        fail(f"Archive holds {size} bytes, snapshot had {expected_size}",
             expected_size=expected_size, actual_size=size)

    actual_sha256 = digest.hexdigest()
    checks["sha256_match"] = actual_sha256 == expected_sha256
    if not checks["sha256_match"]:
        fail("Archive content does not match the snapshot (SHA-256 differs)",
             expected_sha256=expected_sha256, actual_sha256=actual_sha256)

    return errors.success(
        "verify_archive",
        archive=archive_path,
        verified=True,
        archive_size=st.st_size,
        uncompressed_size=size,
        sha256=actual_sha256,
        checks=checks,
    )


def verify_rotation(path, before: os.stat_result, writer_pids=()) -> dict:
    """Check that the active log survived rotation as the SAME, still usable file.

    `before` is the fstat() result taken before rotation. The checks look at the
    PATH again (what users and new writers will open), not at our descriptor:

    exists          - the name still exists (lstat)
    regular_file    - it is a regular file, not a symlink planted meanwhile
    same_inode      - (device, inode) equal to before: truncated, not recreated
    not_deleted     - st_nlink >= 1: the inode still has a name
    mode_preserved  - permission bits unchanged
    owner_preserved - uid/gid unchanged
    appendable      - can be opened O_WRONLY|O_APPEND (a zero-byte write() proves it, adds nothing)
    writers_attached- every writer pid seen before still has the inode open (/proc)
    """
    from .processes import find_open_handles  # local import: processes is optional on non-Linux

    path = os.fspath(path)
    checks = {}

    def fail(message, **details):
        raise RotatorError(errors.ROTATION_VERIFY_FAILED, message, log=path, checks=checks, **details)

    try:
        st = os.lstat(path)
        checks["exists"] = True
    except FileNotFoundError:
        checks["exists"] = False
        fail("The log no longer exists after rotation")

    checks["regular_file"] = stat.S_ISREG(st.st_mode)
    if not checks["regular_file"]:
        fail(f"The log is no longer a regular file ({stat.filemode(st.st_mode)})")

    checks["same_inode"] = (st.st_dev, st.st_ino) == (before.st_dev, before.st_ino)
    if not checks["same_inode"]:
        fail(f"The log is a different file now: inode {before.st_ino} -> {st.st_ino}",
             inode_before=before.st_ino, inode_after=st.st_ino)

    checks["not_deleted"] = st.st_nlink >= 1
    checks["mode_preserved"] = stat.S_IMODE(st.st_mode) == stat.S_IMODE(before.st_mode)
    checks["owner_preserved"] = (st.st_uid, st.st_gid) == (before.st_uid, before.st_gid)
    if not checks["mode_preserved"] or not checks["owner_preserved"]:
        fail("The log's permissions or owner changed during rotation",
             mode_before=stat.filemode(before.st_mode), mode_after=stat.filemode(st.st_mode))

    try:
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
        try:
            os.write(fd, b"")  # permission and file checks happen, no data is added
        finally:
            os.close(fd)
        checks["appendable"] = True
    except OSError as err:
        checks["appendable"] = False
        fail(f"The log cannot be opened for appending: {err.strerror}", errno=err.errno)

    attached = []
    if writer_pids:
        attached = sorted({h["pid"] for h in find_open_handles(path)} & set(writer_pids))
        checks["writers_attached"] = attached == sorted(set(writer_pids))
        if not checks["writers_attached"]:
            fail("Some writer processes no longer have the log open",
                 writers_before=sorted(set(writer_pids)), writers_after=attached)

    return errors.success(
        "verify_rotation",
        log=path,
        verified=True,
        inode=st.st_ino,
        size=st.st_size,
        writers_attached=attached,
        checks=checks,
    )


def watch_log_growth(path, inode: int, timeout: float = 2.0, interval: float = 0.1) -> dict:
    """Sample the log's size and inode for up to `timeout` seconds after rotation.

    Shows that the writer CONTINUES on the same file: the inode stays the
    same and the size grows again from 0. Stops as soon as growth is seen
    (after at least two samples). An idle writer is not an error, so this
    returns `grew: false` instead of raising.
    """
    import time

    path = os.fspath(path)
    samples = []
    start = time.monotonic()
    first_size = None
    while True:
        st = os.stat(path)
        elapsed = round((time.monotonic() - start) * 1000)
        samples.append({"ms": elapsed, "inode": st.st_ino, "size": st.st_size})
        if st.st_ino != inode:
            raise RotatorError(errors.ROTATION_VERIFY_FAILED,
                               f"The log changed inode while watching: {inode} -> {st.st_ino}",
                               log=path, samples=samples)
        if first_size is None:
            first_size = st.st_size
        elif st.st_size > first_size:
            break
        if time.monotonic() - start >= timeout:
            break
        time.sleep(interval)

    return errors.success(
        "watch_log_growth",
        log=path,
        inode=inode,
        grew=samples[-1]["size"] > samples[0]["size"],
        size_start=samples[0]["size"],
        size_end=samples[-1]["size"],
        samples=samples,
    )


def verify_copy(archive_path, expected_sha256: str, expected_size: int) -> dict:
    """Verify an UNCOMPRESSED archive: same checks as verify_archive() minus gzip."""
    archive_path = os.fspath(archive_path)
    checks = {}

    def fail(message, **details):
        raise RotatorError(errors.VERIFY_FAILED, message, archive=archive_path, checks=checks, **details)

    try:
        fd = os.open(archive_path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as err:
        fail(f"Cannot open archive: {err.strerror}", errno=err.errno)

    digest = hashlib.sha256()
    try:
        st = os.fstat(fd)
        checks["regular_file"] = stat.S_ISREG(st.st_mode)
        if not checks["regular_file"]:
            fail("Archive is not a regular file")
        while True:
            chunk = os.read(fd, config.COPY_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    finally:
        os.close(fd)

    checks["size_match"] = st.st_size == expected_size
    if not checks["size_match"]:
        fail(f"Archive holds {st.st_size} bytes, snapshot had {expected_size}",
             expected_size=expected_size, actual_size=st.st_size)
    actual_sha256 = digest.hexdigest()
    checks["sha256_match"] = actual_sha256 == expected_sha256
    if not checks["sha256_match"]:
        fail("Archive content does not match the snapshot (SHA-256 differs)",
             expected_sha256=expected_sha256, actual_sha256=actual_sha256)

    return errors.success("verify_copy", archive=archive_path, verified=True,
                          archive_size=st.st_size, sha256=actual_sha256, checks=checks)
