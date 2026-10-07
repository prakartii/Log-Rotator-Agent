"""Find the processes that currently have a log file open (like `lsof`).

Linux exposes every process's open file descriptors under /proc:

    /proc/<pid>/fd/<n>      symlink to the open file (stat() follows it to the inode)
    /proc/<pid>/fdinfo/<n>  "pos:" = current file offset, "flags:" = open() flags in octal

A descriptor refers to our log when stat() on /proc/<pid>/fd/<n> gives the
same (device, inode) pair as the log. This even finds descriptors whose
file has been deleted, because they still point at the inode.

From the flags we learn whether each writer uses O_APPEND. Truncation is
only safe for O_APPEND writers: a writer without it keeps its old offset
and leaves a zero-filled hole at the start of the file.
Only processes we have permission to inspect are reported (normally our own).
"""

import os

PROC = "/proc"

_ACCESS_MODES = {os.O_RDONLY: "read", os.O_WRONLY: "write", os.O_RDWR: "read-write"}


def _read_fdinfo(pid: str, fd: str) -> dict:
    info = {}
    with open(f"{PROC}/{pid}/fdinfo/{fd}") as f:
        for line in f:
            key, _, value = line.partition(":")
            info[key.strip()] = value.strip()
    return info


def _command(pid: str) -> str:
    try:
        with open(f"{PROC}/{pid}/cmdline", "rb") as f:
            args = f.read().split(b"\0")
        return " ".join(a.decode(errors="replace") for a in args if a) or "?"
    except OSError:
        return "?"


def find_open_handles(path, exclude_self=True) -> list:
    """Return every (process, descriptor) that has `path`'s inode open."""
    if not os.path.isdir(PROC):
        return []  # not Linux; nothing to report
    target = os.stat(path)
    key = (target.st_dev, target.st_ino)
    own_pid = str(os.getpid())
    handles = []

    for pid in os.listdir(PROC):
        if not pid.isdigit() or (exclude_self and pid == own_pid):
            continue
        try:
            fds = os.listdir(f"{PROC}/{pid}/fd")
        except OSError:  # process exited, or belongs to another user
            continue
        for fd in fds:
            try:
                st = os.stat(f"{PROC}/{pid}/fd/{fd}")
                if (st.st_dev, st.st_ino) != key:
                    continue
                fdinfo = _read_fdinfo(pid, fd)
            except OSError:  # descriptor closed while we were looking
                continue
            flags = int(fdinfo.get("flags", "0"), 8)
            handles.append({
                "pid": int(pid),
                "command": _command(pid),
                "fd": int(fd),
                "access": _ACCESS_MODES.get(flags & os.O_ACCMODE, "unknown"),
                "append": bool(flags & os.O_APPEND),
                "offset": int(fdinfo.get("pos", "0")),
            })
    handles.sort(key=lambda h: (h["pid"], h["fd"]))
    return handles


def unsafe_writers(handles: list) -> list:
    """Writers that would leave a hole after truncation (no O_APPEND)."""
    return [h for h in handles if h["access"] != "read" and not h["append"]]
