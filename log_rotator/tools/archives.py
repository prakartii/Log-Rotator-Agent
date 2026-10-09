"""Archive listing: what has been rotated so far.

Archives in rotated_logs/ are named by compressor.archive_name():

    apache_error.log[.<label>].2026-10-07T195312[-N][.gz]

parse_archive_name() splits such a name back into its parts, so the agent
can answer "which archives exist for the apache error log?" without
opening any file.
"""

import re
from datetime import datetime

_ARCHIVE_RE = re.compile(
    r"^(?P<log>.+?)"
    r"(?:\.(?P<label>[A-Za-z0-9_-]+))?"
    r"\.(?P<stamp>\d{4}-\d{2}-\d{2}T\d{6})"
    r"(?:-(?P<copy>\d+))?"
    r"(?P<gz>\.gz)?$"
)


def parse_archive_name(name: str):
    """Return the parts of an archive name, or None if it is not one of ours."""
    match = _ARCHIVE_RE.match(name)
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
