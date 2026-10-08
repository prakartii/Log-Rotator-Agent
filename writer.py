#!/usr/bin/env python3
"""Simulated long-running process that keeps appending to a log file.

This plays the role of a real server (e.g. Apache) during the demo:

    Terminal 1:  python3 writer.py                 # keeps writing
    Terminal 2:  python3 agent.py ...              # rotates the log

OS concepts shown here:

* The log is opened ONCE with os.open() and the same file descriptor is
  used for the whole lifetime of the process, exactly like a real daemon.
* By default the descriptor uses O_APPEND. With O_APPEND the kernel moves
  the file offset to the current end of file before *every* write, so
  after the rotator truncates the file to 0 bytes, the next line lands at
  offset 0 again.
* With --no-append the descriptor keeps its own offset. After truncation
  the writer continues at its OLD offset, and the kernel fills the gap
  with a "hole" of zero bytes (a sparse file). That is why safe rotation
  with truncate requires writers that use O_APPEND.
* Each status line shows the descriptor's offset (lseek), fstat() size,
  inode number and link count. If someone deletes the log instead of
  truncating it, nlink drops to 0: the writer is now writing into a file
  that no longer has a name, and its data is effectively lost.
"""

import argparse
import fcntl
import os
import random
import signal
import sys
import time
from datetime import datetime

from log_rotator import config

MESSAGES = [
    "[core:error] AH00126: Invalid URI in request GET /index.php?id=%27 HTTP/1.1",
    "[proxy:error] AH00957: HTTP: attempt to connect to 127.0.0.1:8080 (backend) failed",
    "[ssl:warn] AH01909: server certificate does NOT include an ID which matches the server name",
    "[authz_core:error] AH01630: client denied by server configuration: /var/www/private",
    "[php:error] PHP Warning:  Undefined variable $user in /var/www/html/login.php on line 42",
    "[mpm_event:notice] AH00489: Apache/2.4.58 (Ubuntu) configured -- resuming normal operations",
    "[core:error] AH00037: Symbolic link not allowed or link target not accessible: /var/www/html/old",
]

_stop = False


def _request_stop(signum, frame):
    global _stop
    _stop = True


def make_line(seq: int, pid: int) -> bytes:
    """Build one Apache-style error line. `seq` lets us check later that no lines were lost."""
    now = datetime.now().strftime("%a %b %d %H:%M:%S.%f %Y")
    client = f"10.0.{random.randint(0, 255)}.{random.randint(1, 254)}:{random.randint(1024, 65535)}"
    message = random.choice(MESSAGES)
    return f"[{now}] [pid {pid}] [client {client}] {message} seq={seq}\n".encode()


def open_log(path: str, append: bool) -> int:
    """Open the log once and return the raw file descriptor."""
    flags = os.O_WRONLY | os.O_CREAT
    if append:
        flags |= os.O_APPEND
    # 0o644 = rw-r--r--, the usual permission for log files.
    return os.open(path, flags, 0o644)


def write_line(fd: int, data: bytes, cooperative: bool = False) -> None:
    """Append one line. A cooperative writer holds a SHARED flock during the write().

    Shared locks never block each other, so many writers still run in
    parallel. Only the rotator's EXCLUSIVE lock (held for a few ms around
    ftruncate) makes this wait - so no line can slip in between the rotator's
    final catch-up and the truncation.
    """
    if cooperative:
        fcntl.flock(fd, fcntl.LOCK_SH)
        try:
            os.write(fd, data)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    else:
        os.write(fd, data)


def prefill(fd: int, megabytes: float, pid: int) -> int:
    """Quickly write `megabytes` of log lines so there is something big to rotate."""
    target = int(megabytes * 1024 * 1024)
    written = 0
    batch = []
    seq = 0
    while written < target:
        seq += 1
        line = make_line(-seq, pid)  # negative seq marks pre-filled history lines
        batch.append(line)
        written += len(line)
        if len(batch) >= 1000:
            os.write(fd, b"".join(batch))
            batch.clear()
    if batch:
        os.write(fd, b"".join(batch))
    return written


def describe(fd: int, path: str) -> str:
    """Status of our descriptor as the kernel sees it."""
    st = os.fstat(fd)
    offset = os.lseek(fd, 0, os.SEEK_CUR)
    status = f"offset={offset} size={st.st_size} inode={st.st_ino} nlink={st.st_nlink}"
    # Compare the inode we are writing to with whatever the path points to now.
    try:
        path_inode = os.stat(path).st_ino
    except FileNotFoundError:
        path_inode = None
    if st.st_nlink == 0:
        status += "  WARNING: our file was DELETED - new lines go to an unnamed inode and are lost"
    elif path_inode != st.st_ino:
        status += f"  WARNING: path now points to a different inode ({path_inode})"
    return status


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Continuously append Apache-style lines to a log file.")
    parser.add_argument("log", nargs="?", default=str(config.LOG_DIR / config.DEFAULT_LOG_NAME),
                        help=f"log file to write (default: logs/{config.DEFAULT_LOG_NAME})")
    parser.add_argument("--rate", type=float, default=20.0,
                        help="lines per second, 0 = as fast as possible (default: 20)")
    parser.add_argument("--count", type=int, default=None,
                        help="stop after this many lines (default: run until Ctrl+C)")
    parser.add_argument("--prefill-mb", type=float, default=0.0,
                        help="write this many MiB of history lines before starting")
    parser.add_argument("--no-append", action="store_true",
                        help="open WITHOUT O_APPEND to demonstrate the sparse-file problem")
    parser.add_argument("--status-interval", type=float, default=1.0,
                        help="seconds between status lines (default: 1)")
    parser.add_argument("--quiet", action="store_true", help="do not print status lines")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    path = os.path.abspath(args.log)
    os.makedirs(os.path.dirname(path), exist_ok=True)

    # Stop cleanly on Ctrl+C (SIGINT) or `kill` (SIGTERM) so the fd is closed properly.
    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)

    pid = os.getpid()
    fd = open_log(path, append=not args.no_append)
    mode = "O_WRONLY|O_CREAT" + ("" if args.no_append else "|O_APPEND")
    print(f"[writer] pid={pid} fd={fd} flags={mode} path={path}", flush=True)

    if args.prefill_mb > 0:
        n = prefill(fd, args.prefill_mb, pid)
        print(f"[writer] prefilled {n} bytes", flush=True)

    delay = 1.0 / args.rate if args.rate > 0 else 0.0
    seq = 0
    last_status = 0.0
    try:
        while not _stop and (args.count is None or seq < args.count):
            seq += 1
            # One write() system call per line: no user-space buffering, so the
            # rotator always sees complete lines on disk.
            os.write(fd, make_line(seq, pid))

            now = time.monotonic()
            if not args.quiet and now - last_status >= args.status_interval:
                print(f"[writer] lines={seq} {describe(fd, path)}", flush=True)
                last_status = now
            if delay:
                time.sleep(delay)
    finally:
        if not args.quiet:
            print(f"[writer] stopping after {seq} lines: {describe(fd, path)}", flush=True)
        os.close(fd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
