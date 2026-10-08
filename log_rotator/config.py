"""Configuration for the Log Rotator Agent.

All paths are absolute so the agent behaves the same no matter which
directory it is launched from (important once a master agent calls us).

Every directory can be overridden with an environment variable, which is
how the tests point the agent at a temporary directory.
"""

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _dir_from_env(var: str, default: Path) -> Path:
    value = os.environ.get(var)
    return Path(value).resolve() if value else default


# Directory holding the active logs that the demo writer appends to.
LOG_DIR = _dir_from_env("LOG_ROTATOR_LOG_DIR", PROJECT_ROOT / "logs")

# Directory where compressed archives are stored.
ARCHIVE_DIR = _dir_from_env("LOG_ROTATOR_ARCHIVE_DIR", PROJECT_ROOT / "rotated_logs")

# Safety: the agent refuses to touch any file outside these directories.
# For the demo only our own logs/ folder is allowed, so a mistaken request
# can never truncate something like /etc/passwd or /var/log/syslog.
# Extra roots can be added as a colon-separated list (like $PATH).
ALLOWED_LOG_ROOTS = [LOG_DIR] + [
    Path(p).resolve()
    for p in os.environ.get("LOG_ROTATOR_EXTRA_ROOTS", "").split(os.pathsep)
    if p
]

# Default log used by writer.py and by requests that name no specific log.
DEFAULT_LOG_NAME = "apache_error.log"

# Friendly names a user (or the master agent) may use for a log file.
LOG_ALIASES = {
    "apache error": "apache_error.log",
    "apache_error": "apache_error.log",
    "apache access": "apache_access.log",
    "apache_access": "apache_access.log",
    "nginx error": "nginx_error.log",
    "nginx access": "nginx_access.log",
    "app": "app.log",
}

# Compression level for gzip (1 = fastest, 9 = smallest).
GZIP_LEVEL = 6

# Chunk size for copying/compressing, so huge logs are never read into memory at once.
COPY_CHUNK_SIZE = 1024 * 1024  # 1 MiB

# Lines a writer appends while the archive is being built are added to the
# archive just before truncation ("catch-up"). Each round shrinks the gap;
# stop after this many rounds so a very busy writer cannot keep us looping.
MAX_CATCHUP_ROUNDS = 5

# Only one rotation of a log may run at a time (flock on a lock file next to
# the log). How long a second rotation waits for the first before giving up
# with ROTATION_IN_PROGRESS; 0 = fail immediately.
ROTATION_LOCK_TIMEOUT = 0.0

# Cooperative writers hold a shared flock on the log while they write. Just
# before truncating, the rotator takes an exclusive flock so no cooperative
# write can slip in between the last catch-up and ftruncate(). If a writer
# keeps the shared lock longer than this, rotation goes ahead without it.
WRITER_LOCK_TIMEOUT = 2.0
