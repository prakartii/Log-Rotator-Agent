"""Log Rotator Agent (agent #15 of the class multi-agent OS system).

Safely rotates log files that are still being written by another process:
the active log is archived (snapshot + gzip + verify) and then shrunk with
ftruncate() *in place*, so its inode and every open file descriptor that a
writer process holds remain valid. The active log is never deleted.

Package layout:
    config.py   - directories, allowed roots and known log names
    tools/      - small, controlled OS-level operations (one concern each)

The public interface for the master/orchestrator agent is exported from
this module as it is implemented.
"""

__version__ = "0.1.0"
AGENT_NAME = "log_rotator"
