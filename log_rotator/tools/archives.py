"""Archive listing: what has been rotated so far.

Archives in rotated_logs/ are named by compressor.archive_name():

    apache_error.log[.<label>].2026-10-07T195312[-N][.gz]

parse_archive_name() splits such a name back into its parts, so the agent
can answer "which archives exist for the apache error log?" without
opening any file.
"""

import os
import re
from datetime import datetime
from pathlib import Path

from .. import config, errors
from .log_info import human_size

_TAIL = (r"(?:\.(?P<label>[A-Za-z0-9_-]+))?"
         r"\.(?P<stamp>\d{4}-\d{2}-\d{2}T\d{6})"
         r"(?:-(?P<copy>\d+))?"
         r"(?P<gz>\.gz)?$")

# A label has no dots, but a log name may ("app.v2.log"), so "x.log.2026-09.<stamp>"
# is ambiguous. Prefer a log name ending in ".log"; otherwise take the longest name.
_ARCHIVE_RES = [re.compile(r"^(?P<log>.+?\.log)" + _TAIL), re.compile(r"^(?P<log>.+)" + _TAIL)]


def parse_archive_name(name: str):
    """Return the parts of an archive name, or None if it is not one of ours."""
    match = next((m for m in (r.match(name) for r in _ARCHIVE_RES) if m), None)
    if not match:
        return None
    try:
        rotated_at = datetime.strptime(match["stamp"], "%Y-%m-%dT%H%M%S")
    except ValueError:  # e.g. month 13
        return None
    return {
        "log_name": match["log"],
        "label": match["label"],
        "rotated_at": rotated_at.isoformat(timespec="seconds"),
        "copy": int(match["copy"]) if match["copy"] else 0,
        "compressed": bool(match["gz"]),
    }


def list_archives(archive_dir=None, log_name: str = None) -> dict:
    """List the archives in the archive directory, newest first.

    log_name limits the list to one log ("apache_error.log"). Hidden files
    (snapshots and temp archives still being written) and symlinks are skipped.
    """
    archive_dir = Path(archive_dir or config.ARCHIVE_DIR)
    archives = []
    try:
        with os.scandir(archive_dir) as entries:
            for entry in entries:
                if entry.name.startswith(".") or not entry.is_file(follow_symlinks=False):
                    continue
                parts = parse_archive_name(entry.name)
                if parts is None or (log_name and parts["log_name"] != log_name):
                    continue
                st = entry.stat(follow_symlinks=False)
                archives.append({"name": entry.name, "path": entry.path, "size": st.st_size,
                                 "size_human": human_size(st.st_size), **parts})
    except FileNotFoundError:
        pass  # nothing has been rotated yet
    except OSError as err:
        raise errors.from_os_error(err, archive_dir) from None
    archives.sort(key=lambda a: (a["rotated_at"], a["copy"]), reverse=True)
    return errors.success("list_archives", archive_dir=str(archive_dir), log_name=log_name,
                          count=len(archives), total_size=sum(a["size"] for a in archives),
                          archives=archives)
