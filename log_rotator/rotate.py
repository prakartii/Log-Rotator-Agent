"""The safe log-rotation pipeline.

    identify log -> open (O_RDWR, validated) -> fstat -> snapshot -> compress
        -> verify archive -> catch up new lines -> ftruncate(fd, 0) -> structured result

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
from contextlib import ExitStack, contextmanager
from datetime import datetime
from pathlib import Path

from . import config, errors
from .errors import RotatorError
from .tools import compressor, locking, snapshot, truncator, verifier
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
               label=None, log_dir=None, allowed_roots=None, now: datetime = None,
               watch_writer: float = 0.0, lock_timeout: float = None) -> dict:
    """Archive the active log and empty it in place.

    log          - name, alias, description or path of the log (default: apache_error.log)
    archive_dir  - where archives go (default: rotated_logs/)
    compress     - gzip the archive (True) or keep a plain copy (False)
    truncate     - False = archive only, leave the log as it is
    dry_run      - validate and report what would happen, change nothing
    label        - optional tag in the archive name, e.g. "2026-09"
    watch_writer - seconds to watch the log grow again after rotation (0 = don't wait)
    lock_timeout - seconds to wait if another rotation of this log is running
                   (default: config.ROTATION_LOCK_TIMEOUT)
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
    locks = ExitStack()  # released in `finally`, after the log descriptor is closed
    try:
        with steps.step("identify") as s:
            path = identify_log(log, log_dir=log_dir, allowed_roots=roots)["log"]
            s["log"] = path
        info["log"] = path

        if not dry_run:
            # One rotation per log at a time; a second one gets ROTATION_IN_PROGRESS.
            with steps.step("lock") as s:
                s.update(locks.enter_context(locking.rotation_lock(path, timeout=lock_timeout)))

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
        name = compressor.archive_name(path, when=now, label=label, compressed=compress)

        if dry_run:
            info.update(would_archive_to=str(archive_dir / name), would_truncate=truncate, truncated=False)
            return {"status": "success", **info, "steps": steps.items}

        archive_dir = compressor.ensure_archive_dir(archive_dir)
        snap_path = archive_dir / f".{name}.{os.getpid()}.snapshot"

        with steps.step("snapshot") as s:
            snap = snapshot.snapshot_log(fd, snap_path, length=before.st_size)
            s["bytes"] = snap["bytes"]

        if compress:
            with steps.step("compress") as s:
                archive_path = compressor.compress_log(snap_path, archive_dir=archive_dir, name=name,
                                                       original_name=os.path.basename(path))["archive"]
                s["archive"] = archive_path
            with steps.step("verify_archive"):
                verifier.verify_archive(archive_path, snap["sha256"], snap["bytes"])
                archive_verified = True
        else:
            # Uncompressed: the fsync()ed snapshot itself becomes the archive.
            with steps.step("publish") as s:
                archive_path = str(compressor.publish_archive(snap_path, archive_dir, name))
                s["archive"] = archive_path
            with steps.step("verify_archive"):
                verifier.verify_copy(archive_path, snap["sha256"], snap["bytes"])
                archive_verified = True

        # From here on the archive holds a verified copy of the log. Record where it is,
        # so even an error result after truncation tells the caller where the data went.
        info["archive"] = archive_path

        archived = snap["bytes"]
        caught_up = 0
        bytes_lost = 0
        if truncate:
            append = compressor.append_gzip_member if compress else compressor.append_raw
            with steps.step("catch_up") as s:
                caught_up, rounds = _catch_up(fd, archive_path, archived, append)
                archived += caught_up
                s.update(bytes=caught_up, rounds=rounds)

            # Exclusive flock on the log: cooperative writers wait until ftruncate() is done.
            writer_guard = ExitStack()
            locks.callback(writer_guard.close)  # also released if a step below fails
            with steps.step("lock_writers") as s:
                writer_lock = writer_guard.enter_context(locking.writer_lock(fd))
                s.update(writer_lock)
            info["writer_lock"] = writer_lock
            with writer_guard:
                if writer_lock["acquired"]:
                    # Cooperative writers are paused now: whatever is in the log is
                    # complete and nothing new can arrive before ftruncate().
                    with steps.step("final_catch_up") as s:
                        extra, rounds = _catch_up(fd, archive_path, archived, append)
                        archived += extra
                        caught_up += extra
                        s.update(bytes=extra, rounds=rounds)
                with steps.step("truncate") as s:
                    trunc = truncator.truncate_log(fd, 0)
                    s["bytes_removed"] = trunc["bytes_removed"]
            truncated = True
            info["size_after_truncate"] = trunc["size_after"]  # always 0; size_after may already be > 0
            # Bytes written between the last catch-up and ftruncate() could not be saved.
            # Report them honestly (normally 0; the lock-cooperating writer makes it always 0).
            bytes_lost = max(trunc["size_before"] - archived, 0)

            # Never report success without checking the result: same inode, still
            # writable, and every writer still attached to it.
            writer_pids = [h["pid"] for h in handles if h["access"] != "read"]
            with steps.step("verify_rotation") as s:
                rotation_check = verifier.verify_rotation(path, before, writer_pids=writer_pids)
                s["writers_attached"] = rotation_check["writers_attached"]
            info["rotation_checks"] = rotation_check["checks"]

            if watch_writer > 0 and writer_pids:
                with steps.step("watch_writer") as s:
                    growth = verifier.watch_log_growth(path, before.st_ino, timeout=watch_writer)
                    s.update(grew=growth["grew"], samples=len(growth["samples"]))
                info["writer_continues"] = growth["grew"]
                info["growth"] = {k: growth[k] for k in ("size_start", "size_end", "samples")}
                if not growth["grew"]:
                    info["warnings"].append(
                        f"log did not grow within {watch_writer}s after rotation (writer idle?)")

        after = os.fstat(fd)
        archive_size = os.stat(archive_path).st_size
        info.update(
            archive=archive_path,
            compressed=compress,
            archive_size=archive_size,
            compression_ratio=round(archive_size / archived, 4) if archived else 0.0,
            bytes_archived=archived,
            caught_up_bytes=caught_up,
            bytes_lost=bytes_lost,
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
        locks.close()


def _catch_up(fd: int, archive_path, archived: int, append) -> tuple:
    """Archive what the writer appended after the snapshot, just before truncating.

    Snapshot + compress + verify take time (hundreds of ms for big logs) and
    the writer keeps appending. Without this step those lines would be cut
    off by ftruncate(). Each round reads only the new tail with pread() and
    adds it to the archive as an extra gzip member; rounds get shorter and
    shorter, so the remaining gap before ftruncate() is tiny.
    """
    caught_up = 0
    rounds = 0
    while rounds < config.MAX_CATCHUP_ROUNDS:
        size = os.fstat(fd).st_size
        if size <= archived:
            break
        delta = _pread_all(fd, size - archived, archived)
        if not delta:
            break
        append(archive_path, delta)
        archived += len(delta)
        caught_up += len(delta)
        rounds += 1
    return caught_up, rounds


def _pread_all(fd: int, length: int, offset: int) -> bytes:
    """pread() may return fewer bytes than asked; keep reading until `length` or EOF."""
    parts = []
    while length > 0:
        chunk = os.pread(fd, length, offset)
        if not chunk:
            break
        parts.append(chunk)
        offset += len(chunk)
        length -= len(chunk)
    return b"".join(parts)


def _remove_quietly(path) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
