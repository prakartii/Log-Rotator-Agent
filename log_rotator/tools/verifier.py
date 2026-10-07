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
