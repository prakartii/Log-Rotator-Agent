"""Log Rotator Agent (agent #15 of the class multi-agent OS system).

Safely rotates log files that are still being written by another process:
the active log is archived (snapshot + gzip + verify) and then shrunk with
ftruncate() *in place*, so its inode and every open file descriptor that a
writer process holds remain valid. The active log is never deleted.

Package layout:
    config.py      - directories, allowed roots and known log names
    tools/         - small, controlled OS-level operations (one concern each)
    rotate.py      - the safe rotation pipeline
    agent_tools.py - the tool interface for the master/orchestrator agent

The master agent only needs two functions:

    from log_rotator import describe_tools, call_tool
    describe_tools()                                    # tool names + JSON schemas
    call_tool("rotate_log", {"log": "apache error"})   # -> structured result dict
"""

__version__ = "0.1.0"
AGENT_NAME = "log_rotator"

from .agent_tools import call_tool, describe_tools  # noqa: E402

__all__ = ["AGENT_NAME", "call_tool", "describe_tools"]
