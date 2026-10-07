"""The safe log-rotation pipeline.

    identify log -> open (O_RDWR, validated) -> fstat -> snapshot -> compress
        -> verify archive -> ftruncate(fd, 0) -> structured result

Rules that make it safe:

* The log is opened ONCE and every later step uses that descriptor, so all
  steps act on the same inode even if the path is renamed meanwhile.
* The log is only READ until the archive has been verified. If snapshot,
  compression or verification fails, we stop: the log is not truncated
  and the result says "log_unchanged": true.
* Truncation uses ftruncate() on the descriptor; the log is never deleted.
* Temporary files (the snapshot, a failed archive) are always cleaned up.

rotate_log() never raises for expected problems; it always returns a dict,
which is what the master agent receives.
"""

import os
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from . import config, errors
from .errors import RotatorError
from .tools import compressor, snapshot, truncator, verifier
from .tools.log_info import human_size, identify_log
from .tools.processes import find_open_handles, unsafe_writers
from .tools.safety import open_validated


class _StepLog:
    """Records each pipeline step with its outcome and duration (shown in the demo)."""

    def __init__(self):
        self.items = []

    @contextmanager
    def step(self, name):
        entry = {"step": name}
        self.items.append(entry)
        start = time.perf_counter()
        try:
            yield entry
            entry["ok"] = True
        except BaseException as err:
            entry["ok"] = False
            if isinstance(err, RotatorError):
                entry["error_code"] = err.code
            raise
        finally:
            entry["ms"] = round((time.perf_counter() - start) * 1000, 2)


def rotate_log(log=None, archive_dir=None, compress=True, truncate=True, dry_run=False,
               label=None, log_dir=None, allowed_roots=None, now: datetime = None) -> dict:
    """Archive the active log and empty it in place.

    log          - name, alias, description or path of the log (default: apache_error.log)
    archive_dir  - where archives go (default: rotated_logs/)
    truncate     - False = archive only, leave the log as it is
    dry_run      - validate and report what would happen, change nothing
    label        - optional tag in the archive name, e.g. "2026-09"
    """
    steps = _StepLog()
    info = {"action": "rotate", "dry_run": dry_run}
    archive_dir = Path(archive_dir or config.ARCHIVE_DIR)
    roots = allowed_roots if allowed_roots is not None else ([Path(log_dir)] if log_dir else None)

    fd = None
    snap_path = None
    archive_path = None
    archive_verified = False
    truncated = False
    try:
        with steps.step("identify") as s:
            path = identify_log(log, log_dir=log_dir, allowed_roots=roots)["log"]
            s["log"] = path
        info["log"] = path

        with steps.step("open"):
            # O_RDWR: ftruncate() later requires a descriptor opened for writing.
            fd, _ = open_validated(path, os.O_RDWR, allowed_roots=roots)
        before = os.fstat(fd)
        handles = find_open_handles(path)
        info.update(
            inode_before=before.st_ino,
            original_size=before.st_size,
            original_size_human=human_size(before.st_size),
            open_by=handles,
            warnings=[f"pid {h['pid']} writes without O_APPEND; truncation will leave a hole"
                      for h in unsafe_writers(handles)],
        )
        name = compressor.archive_name(path, when=now, label=label)

        if dry_run:
            info.update(would_archive_to=str(archive_dir / name), would_truncate=truncate, truncated=False)
            return {"status": "success", **info, "steps": steps.items}

        archive_dir = compressor.ensure_archive_dir(archive_dir)
        snap_path = archive_dir / f".{name}.{os.getpid()}.snapshot"

        with steps.step("snapshot") as s:
            snap = snapshot.snapshot_log(fd, snap_path, length=before.st_size)
            s["bytes"] = snap["bytes"]

        with steps.step("compress") as s:
            archive = compressor.compress_log(snap_path, archive_dir=archive_dir, name=name,
                                              original_name=os.path.basename(path))
            archive_path = archive["archive"]
            s["archive"] = archive_path

        with steps.step("verify_archive"):
            verifier.verify_archive(archive_path, snap["sha256"], snap["bytes"])
            archive_verified = True

        if truncate:
            with steps.step("truncate") as s:
                trunc = truncator.truncate_log(fd, 0)
                s["bytes_removed"] = trunc["bytes_removed"]
            truncated = True

        after = os.fstat(fd)
        info.update(
            archive=archive_path,
            archive_size=archive["archive_size"],
            compression_ratio=archive["compression_ratio"],
            bytes_archived=snap["bytes"],
            sha256=snap["sha256"],
            truncated=truncated,
            inode_after=after.st_ino,
            inode_preserved=(after.st_dev, after.st_ino) == (before.st_dev, before.st_ino),
            size_after=after.st_size,
        )
        return {"status": "success", **info, "steps": steps.items}

    except (RotatorError, OSError) as err:
        if isinstance(err, OSError):
            err = errors.from_os_error(err)
        if archive_path and not archive_verified:
            _remove_quietly(archive_path)  # an unverified archive must not look like a good one
            info["archive_removed"] = archive_path
        result = err.to_dict(action="rotate")
        result.update({k: v for k, v in info.items() if k not in result})
        result.update(truncated=truncated, log_unchanged=not truncated, steps=steps.items)
        return result

    finally:
        if snap_path is not None:
            _remove_quietly(snap_path)
        if fd is not None:
            os.close(fd)


def _remove_quietly(path) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
