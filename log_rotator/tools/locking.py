"""File locking with flock(): one rotation at a time, and cooperative writers.

Two different locks are used, for two different problems:

1. The ROTATION lock - two rotators must never work on the same log at the
   same time (both would archive the same bytes and the second ftruncate()
   could cut off lines the first one already counted as safe). Each rotator
   takes an EXCLUSIVE flock on a small lock file next to the log:

       logs/apache_error.log  ->  logs/.apache_error.log.rotate.lock

   The lock file is never deleted. If it were, a second rotator could create
   a NEW lock file (new inode) and lock that one, while the first still holds
   the lock on the old, deleted inode - both would think they own the lock.

2. The WRITER lock - a cooperative writer takes a SHARED flock on the log
   itself around every write(). Shared locks do not block each other, so
   several writers can still append at once. Just before ftruncate() the
   rotator takes an EXCLUSIVE flock on the log: the kernel makes it wait
   until in-flight writes finish and then holds new writes back until the
   log has been truncated. Nothing can land between the final catch-up and
   ftruncate(), so no line is lost.

flock() locks are ADVISORY: they only work between processes that use them.
A writer that never calls flock() is not blocked (that is why the catch-up
step still exists). Locks belong to the open file description, so the kernel
releases them automatically when the process closes the descriptor or dies -
a crashed rotator can never leave a log locked forever.
"""

import os
from pathlib import Path


def lock_path_for(log_path) -> Path:
    """The rotation lock file for `log_path`: a hidden file in the same directory."""
    log_path = Path(os.fspath(log_path))
    return log_path.with_name(f".{log_path.name}.rotate.lock")
