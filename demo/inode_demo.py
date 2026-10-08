#!/usr/bin/env python3
"""Presentation demo: the inode stays the same, the writer keeps running.

    python3 demo/inode_demo.py                      # uses logs/ and rotated_logs/
    python3 demo/inode_demo.py --dir ~/rotator-demo # Linux filesystem: small, readable inode numbers
    python3 demo/inode_demo.py --compare            # also show the wrong way (rm + create)

What it shows:
    BEFORE   inode = X   size = large    writer running
    AFTER    inode = X   size = 0        writer running
    +t       inode = X   size growing    writer running
"""

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from log_rotator.rotate import rotate_log  # noqa: E402
from log_rotator.tools.log_info import human_size  # noqa: E402


def row(label, path, writer):
    st = os.stat(path)
    alive = "running" if writer.poll() is None else f"EXITED ({writer.returncode})"
    print(f"  {label:<8} inode = {st.st_ino:<20} size = {human_size(st.st_size):>10}   writer pid {writer.pid}: {alive}")


def wait_for_size(path, size, writer, timeout=60.0):
    """Wait until the writer has filled `path` to `size` bytes; fail instead of hanging."""
    deadline = time.monotonic() + timeout
    while not path.exists() or os.stat(path).st_size < size:
        if writer.poll() is not None:
            raise SystemExit(f"writer exited early with code {writer.returncode}")
        if time.monotonic() > deadline:
            raise SystemExit(f"timed out after {timeout:g}s waiting for {path} to reach {size} bytes")
        time.sleep(0.05)


def wrong_way(log_dir: Path, rate: float) -> None:
    """For contrast: 'rotate' by deleting and recreating the log (what NOT to do)."""
    log = log_dir / "wrong_way.log"
    log.unlink(missing_ok=True)
    print(f"\n[X] The WRONG way for comparison: rm + create  ({log.name})")
    writer = subprocess.Popen([sys.executable, str(PROJECT_ROOT / "writer.py"), str(log),
                               "--prefill-mb", "1", "--rate", str(rate), "--quiet"],
                              stdout=subprocess.DEVNULL)
    try:
        while not log.exists() or os.stat(log).st_size < 1024 * 1024:
            time.sleep(0.05)
        row("BEFORE", log, writer)

        log.unlink()              # delete the active log ...
        log.write_bytes(b"")      # ... and create a new, empty one with the same name
        time.sleep(1)

        row("+1s", log, writer)
        target = os.readlink(f"/proc/{writer.pid}/fd/3")
        print(f"  writer's fd 3 -> {target}")
        print("  The name points to a NEW inode that stays empty. The writer still writes to the")
        print("  OLD inode, which has no name any more: its lines are lost and its disk space is")
        print("  not freed until the writer is restarted.")
    finally:
        writer.terminate()
        writer.wait(timeout=5)
        log.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dir", help="base directory for logs/ and rotated_logs/ (default: project folder)")
    parser.add_argument("--prefill-mb", type=float, default=20, help="size of the log before rotation")
    parser.add_argument("--rate", type=float, default=200, help="writer lines per second")
    parser.add_argument("--compare", action="store_true",
                        help="afterwards, show the wrong way (rm + create) for comparison")
    args = parser.parse_args()

    base = Path(args.dir).expanduser().resolve() if args.dir else PROJECT_ROOT
    log_dir, archive_dir = base / "logs", base / "rotated_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log = log_dir / "apache_error.log"
    log.unlink(missing_ok=True)

    print(f"\n[1] Starting writer: appends to {log} with O_APPEND ({args.rate:g} lines/s)")
    writer = subprocess.Popen([sys.executable, str(PROJECT_ROOT / "writer.py"), str(log),
                               "--prefill-mb", str(args.prefill_mb), "--rate", str(args.rate), "--quiet"],
                              stdout=subprocess.DEVNULL)
    try:
        target = args.prefill_mb * 1024 * 1024
        while not log.exists() or os.stat(log).st_size < target:
            time.sleep(0.05)
        time.sleep(0.5)

        print("\n[2] Before rotation")
        row("BEFORE", log, writer)

        print("\n[3] Rotating: snapshot -> gzip -> verify -> catch up -> ftruncate(fd, 0) -> verify")
        result = rotate_log(str(log), log_dir=log_dir, archive_dir=archive_dir, watch_writer=3)
        if result["status"] != "success":
            print(f"  ROTATION FAILED: {result['error_code']}: {result['message']}")
            return 1
        for step in result["steps"]:
            print(f"      {step['step']:<16} {'ok' if step['ok'] else 'FAILED':<6} {step['ms']:>8.2f} ms")

        print("\n[4] After rotation")
        print(f"  {'AFTER':<8} inode = {result['inode_after']:<20} size = "
              f"{human_size(result['size_after_truncate']):>10}   (at ftruncate; inode preserved: "
              f"{result['inode_preserved']})")
        for sample in result.get("growth", {}).get("samples", []):
            print(f"  +{sample['ms']:<5}ms inode = {sample['inode']:<20} size = {human_size(sample['size']):>10}")
        time.sleep(1)
        row("+1s", log, writer)

        print("\n[5] Archive")
        print(f"  {result['archive']}")
        print(f"  {human_size(result['bytes_archived'])} -> {human_size(result['archive_size'])} "
              f"(ratio {result['compression_ratio']}), caught up {result['caught_up_bytes']} bytes, "
              f"lost {result['bytes_lost']} bytes")
        print(f"\n  The writer never stopped and still writes to inode {result['inode_before']}.\n")
    finally:
        writer.terminate()
        writer.wait(timeout=5)

    if args.compare:
        wrong_way(log_dir, args.rate)
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
