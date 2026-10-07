"""Log discovery and metadata tools.

identify_log()  - turn a name like "apache error logs" into a validated path
list_logs()     - list the active logs in the log directory
get_log_info()  - inode, size, permissions, owner and timestamps of one log

Metadata comes from the inode: open() the file, fstat() the descriptor,
close() it. fstat() on an open descriptor describes exactly the file we
opened, even if the path is renamed or replaced at the same moment.
"""

import grp
import os
import pwd
import stat
from datetime import datetime
from pathlib import Path

from .. import config
from .. import errors
from ..errors import RotatorError
from .processes import find_open_handles, unsafe_writers
from .safety import open_validated, validate_log_path

# Words that describe "a log" rather than name one: "the apache error logs".
_FILLER_WORDS = {"the", "log", "logs", "file", "files", "current", "active"}


def human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).isoformat(timespec="seconds")


def list_logs(log_dir=None) -> dict:
    """List regular files (no symlinks, no hidden files) in the log directory."""
    log_dir = Path(log_dir or config.LOG_DIR)
    logs = []
    try:
        with os.scandir(log_dir) as entries:
            for entry in entries:
                # follow_symlinks=False: a symlink is never reported as a log
                if entry.name.startswith(".") or not entry.is_file(follow_symlinks=False):
                    continue
                st = entry.stat(follow_symlinks=False)
                logs.append({
                    "name": entry.name,
                    "path": entry.path,
                    "size": st.st_size,
                    "size_human": human_size(st.st_size),
                    "inode": st.st_ino,
                    "modified": _iso(st.st_mtime),
                })
    except OSError as err:
        raise errors.from_os_error(err, log_dir) from None
    logs.sort(key=lambda log: log["name"])
    return errors.success("list_logs", log_dir=str(log_dir), count=len(logs), logs=logs)


def _normalise(query: str) -> str:
    words = query.strip().lower().replace("-", " ").split()
    return " ".join(w for w in words if w not in _FILLER_WORDS)


def identify_log(query=None, log_dir=None, allowed_roots=None) -> dict:
    """Find the active log the user means.

    Accepts a path ("logs/apache_error.log"), a file name ("apache_error.log"),
    an alias ("apache error") or a loose description ("the apache error logs").
    With no query the default log is used.
    """
    log_dir = Path(log_dir or config.LOG_DIR)
    if allowed_roots is None:
        allowed_roots = [log_dir] if log_dir != config.LOG_DIR else None

    def found(path, matched_by):
        info = validate_log_path(path, allowed_roots=allowed_roots, need_write=False)
        return errors.success("identify_log", query=query, log=info["path"], matched_by=matched_by)

    if query is None or not str(query).strip():
        return found(log_dir / config.DEFAULT_LOG_NAME, "default")

    query = str(query)
    if os.sep in query:  # looks like a path
        return found(query, "path")

    name = _normalise(query)
    candidates = []
    if name in config.LOG_ALIASES:
        candidates.append((config.LOG_ALIASES[name], "alias"))
    underscored = name.replace(" ", "_")
    candidates += [(query.strip(), "name"), (underscored, "name"), (underscored + ".log", "name")]
    for candidate, matched_by in candidates:
        if candidate and os.path.lexists(log_dir / candidate):
            return found(log_dir / candidate, matched_by)

    # Fuzzy match: every word of the request appears in the file name.
    available = [log["name"] for log in list_logs(log_dir)["logs"]]
    tokens = name.replace("_", " ").split()
    matches = [n for n in available if tokens and all(t in n.lower() for t in tokens)]
    if len(matches) == 1:
        return found(log_dir / matches[0], "fuzzy")
    if len(matches) > 1:
        raise RotatorError(errors.AMBIGUOUS_LOG, f"'{query}' matches several logs: {', '.join(matches)}",
                           query=query, candidates=matches)
    raise RotatorError(errors.LOG_NOT_FOUND, f"No log matching '{query}' in {log_dir}",
                       query=query, available_logs=available)


def _has_hole(fd: int, st) -> bool:
    """True if the file contains a hole (a never-written, zero-filled gap).

    lseek(fd, 0, SEEK_HOLE) asks the filesystem for the first hole; every file
    has an implicit hole at EOF, so a result before st_size means a real gap.
    Holes appear when a writer without O_APPEND continues after truncation.
    """
    if st.st_size == 0:
        return False
    try:
        return os.lseek(fd, 0, os.SEEK_HOLE) < st.st_size
    except (OSError, AttributeError):  # filesystem/OS without SEEK_HOLE support
        return 0 < st.st_blocks * 512 < st.st_size


def get_log_info(path, allowed_roots=None, include_processes=True) -> dict:
    """Return inode-level metadata for one log, read with open() + fstat() + close().

    With include_processes, also lists the processes that have the log open
    (from /proc) and warns about writers that do not use O_APPEND.
    """
    fd, _ = open_validated(path, os.O_RDONLY, allowed_roots=allowed_roots)
    try:
        st = os.fstat(fd)
        sparse = _has_hole(fd, st)
    finally:
        os.close(fd)

    try:
        owner = pwd.getpwuid(st.st_uid).pw_name
    except KeyError:
        owner = str(st.st_uid)
    try:
        group = grp.getgrgid(st.st_gid).gr_name
    except KeyError:
        group = str(st.st_gid)

    real_path = os.path.realpath(path)
    disk_usage = st.st_blocks * 512  # st_blocks is always counted in 512-byte units

    extra = {}
    if include_processes:
        handles = find_open_handles(real_path)
        extra["open_by"] = handles
        extra["warnings"] = [
            f"pid {h['pid']} writes without O_APPEND; truncating would leave a hole of "
            f"{h['offset']} zero bytes" for h in unsafe_writers(handles)
        ]

    return errors.success(
        "get_log_info",
        log=real_path,
        name=os.path.basename(real_path),
        inode=st.st_ino,
        device=st.st_dev,
        size=st.st_size,
        size_human=human_size(st.st_size),
        # Allocated space can be smaller than the size for sparse files with holes.
        disk_usage=disk_usage,
        sparse=sparse,
        mode=stat.filemode(st.st_mode),
        mode_octal=oct(stat.S_IMODE(st.st_mode)),
        owner=owner,
        uid=st.st_uid,
        group=group,
        gid=st.st_gid,
        nlink=st.st_nlink,
        modified=_iso(st.st_mtime),
        accessed=_iso(st.st_atime),
        changed=_iso(st.st_ctime),
        readable=os.access(real_path, os.R_OK),
        writable=os.access(real_path, os.W_OK),
        **extra,
    )
