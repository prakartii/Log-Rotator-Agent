"""Compression: turn a snapshot into a gzip archive in rotated_logs/.

The archive must either exist completely or not at all, so it is built in
steps that can be explained with system calls:

1. Write the compressed data to a hidden temporary file in the archive
   directory (O_CREAT | O_EXCL, mode 0640).
2. fsync() the temporary file so its data is on disk.
3. Publish it under its final name with link(). Unlike rename(), which
   silently REPLACES an existing file, link() fails with EEXIST, so an
   older archive can never be overwritten. On a name clash we try
   name-1.gz, name-2.gz, ... Then the temporary name is unlink()ed.
4. fsync() the directory, which makes the new directory entry durable.

Nobody ever sees a half-written archive under the final name. If anything
fails, the temporary file is removed and the source is left untouched.
"""

import errno
import gzip
import hashlib
import os
import re
import time
from datetime import datetime
from pathlib import Path

from .. import config
from .. import errors
from ..errors import RotatorError

_LABEL_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_MAX_NAME_ATTEMPTS = 100


def archive_name(log_path, when: datetime = None, label: str = None) -> str:
    """apache_error.log -> apache_error.log[.<label>].2026-10-07T195312.gz"""
    if label is not None and not _LABEL_RE.match(label):
        raise RotatorError(errors.INVALID_REQUEST,
                           f"Archive label may only contain letters, digits, '-' and '_': {label!r}",
                           label=label)
    stamp = (when or datetime.now()).strftime("%Y-%m-%dT%H%M%S")
    parts = [os.path.basename(os.fspath(log_path))] + ([label] if label else []) + [stamp]
    return ".".join(parts) + ".gz"


def ensure_archive_dir(archive_dir=None) -> Path:
    """Create the archive directory (mode 0750) if needed and check it is a real directory."""
    archive_dir = Path(archive_dir or config.ARCHIVE_DIR)
    try:
        archive_dir.mkdir(mode=0o750, parents=True, exist_ok=True)
    except OSError as err:
        raise errors.from_os_error(err, archive_dir) from None
    if archive_dir.is_symlink() or not archive_dir.is_dir():
        raise RotatorError(errors.NOT_A_REGULAR_FILE,
                           f"Archive location {archive_dir} is not a real directory",
                           path=str(archive_dir))
    if not os.access(archive_dir, os.W_OK | os.X_OK):
        raise RotatorError(errors.PERMISSION_DENIED,
                           f"No write permission on archive directory {archive_dir}",
                           path=str(archive_dir))
    return archive_dir


def compress_log(src_path, archive_dir=None, name: str = None, original_name: str = None,
                 level: int = None) -> dict:
    """gzip `src_path` into `archive_dir`/`name` and return a structured result.

    `original_name` is stored in the gzip header (shown by `gunzip -l -N`).
    The SHA-256 of the *uncompressed* data is returned so the verifier can
    check the archive against the snapshot.
    """
    archive_dir = ensure_archive_dir(archive_dir)
    src_path = os.fspath(src_path)
    name = name or archive_name(original_name or src_path)
    if os.sep in name or name.startswith("."):
        raise RotatorError(errors.INVALID_REQUEST, f"Invalid archive name: {name!r}", name=name)
    level = config.GZIP_LEVEL if level is None else level

    tmp_path = archive_dir / f".{name}.{os.getpid()}.tmp"
    digest = hashlib.sha256()
    original_size = 0
    try:
        src_fd = os.open(src_path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            tmp_fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
            with os.fdopen(tmp_fd, "wb") as raw:
                with gzip.GzipFile(filename=original_name or os.path.basename(src_path), mode="wb",
                                   compresslevel=level, fileobj=raw, mtime=int(time.time())) as gz:
                    while True:
                        chunk = os.read(src_fd, config.COPY_CHUNK_SIZE)
                        if not chunk:
                            break
                        gz.write(chunk)
                        digest.update(chunk)
                        original_size += len(chunk)
                raw.flush()
                os.fsync(raw.fileno())  # data on disk before the archive gets its real name
        finally:
            os.close(src_fd)
        final_path = _publish(tmp_path, archive_dir, name)
    except BaseException as err:
        _remove_quietly(tmp_path)
        if isinstance(err, RotatorError):
            raise
        if isinstance(err, OSError):
            raise RotatorError(errors.COMPRESSION_FAILED, f"Compression failed: {err.strerror or err}",
                               source=src_path, errno=err.errno) from None
        if isinstance(err, Exception):
            raise RotatorError(errors.COMPRESSION_FAILED, f"Compression failed: {err}",
                               source=src_path) from None
        raise

    archive_size = os.stat(final_path).st_size
    return errors.success(
        "compress_log",
        archive=str(final_path),
        archive_size=archive_size,
        original_size=original_size,
        compression_ratio=round(archive_size / original_size, 4) if original_size else 0.0,
        sha256=digest.hexdigest(),
    )


def _publish(tmp_path: Path, archive_dir: Path, name: str) -> Path:
    """Give the finished temp file its final name without ever replacing an existing file."""
    stem = name[:-3] if name.endswith(".gz") else name
    for attempt in range(_MAX_NAME_ATTEMPTS):
        candidate = archive_dir / (name if attempt == 0 else f"{stem}-{attempt}.gz")
        try:
            os.link(tmp_path, candidate)  # atomic; fails with EEXIST instead of overwriting
        except FileExistsError:
            continue
        except OSError as err:
            if err.errno not in (errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP):
                raise
            # Filesystem without hard links: rename(), after checking the name is free.
            if os.path.lexists(candidate):
                continue
            os.rename(tmp_path, candidate)
        else:
            os.unlink(tmp_path)
        _fsync_dir(archive_dir)
        return candidate
    raise RotatorError(errors.COMPRESSION_FAILED, f"Could not find a free archive name for {name}",
                       name=name)


def _fsync_dir(directory: Path) -> None:
    """fsync() a directory so a newly created name in it survives a crash."""
    try:
        dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass  # some filesystems (e.g. Windows drives under WSL) do not support it
    finally:
        os.close(dir_fd)


def _remove_quietly(path) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
