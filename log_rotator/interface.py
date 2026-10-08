"""Tool interface for the master agent.

The master agent never imports our modules directly. It asks for the list
of tools (tool_specs(), JSON-schema style, like an LLM function-calling API)
and then calls one by name with a dict of arguments:

    call_tool("rotate_log", {"log": "the apache error logs", "dry_run": True})

Rules of the interface:

* call_tool() never raises for expected problems. Every result is a dict
  with "status": "success" or "status": "error" plus a stable error_code.
* Arguments are checked against the tool's schema before anything runs:
  unknown tools, unknown arguments and wrong types give INVALID_REQUEST.
* Directories are NOT arguments. Where logs and archives live comes only from
  config (environment variables), so a request can never point the agent at
  a different directory such as /etc or /var/log.
"""

import math
import os
from pathlib import Path

from . import config, errors
from .errors import RotatorError
from .rotate import rotate_log
from .tools import log_info
from .tools.log_info import human_size

_LOG_ARG = {
    "type": "string",
    "description": "Log name, alias, description or path, e.g. 'apache_error.log' or "
                   "'the apache error logs'. Default: " + config.DEFAULT_LOG_NAME,
}


def _list_logs() -> dict:
    return log_info.list_logs()


def _identify_log(log=None) -> dict:
    return log_info.identify_log(log)


def _get_log_info(log=None) -> dict:
    path = log_info.identify_log(log)["log"]
    return log_info.get_log_info(path)


def _list_archives(log=None) -> dict:
    """Archives in rotated_logs/, newest first. Hidden files (temp/snapshot files) are skipped."""
    archive_dir = Path(config.ARCHIVE_DIR)
    prefix = None
    if log is not None:
        # The log may be resolved by alias; its archives start with "<log name>."
        prefix = os.path.basename(log_info.identify_log(log)["log"]) + "."
    archives = []
    try:
        with os.scandir(archive_dir) as entries:
            for entry in entries:
                if entry.name.startswith(".") or not entry.is_file(follow_symlinks=False):
                    continue
                if prefix and not entry.name.startswith(prefix):
                    continue
                st = entry.stat(follow_symlinks=False)
                archives.append({
                    "name": entry.name,
                    "path": entry.path,
                    "size": st.st_size,
                    "size_human": human_size(st.st_size),
                    "compressed": entry.name.endswith(".gz"),
                    "modified": log_info._iso(st.st_mtime),
                    "_mtime": st.st_mtime,
                })
    except FileNotFoundError:
        pass  # no rotation has happened yet
    except OSError as err:
        raise errors.from_os_error(err, archive_dir) from None
    archives.sort(key=lambda a: (a.pop("_mtime"), a["name"]), reverse=True)
    return errors.success("list_archives", archive_dir=str(archive_dir), count=len(archives),
                          archives=archives)


def _rotate_log(log=None, compress=True, truncate=True, dry_run=False, label=None,
                watch_writer=0.0, lock_timeout=None) -> dict:
    return rotate_log(log, compress=compress, truncate=truncate, dry_run=dry_run, label=label,
                      watch_writer=watch_writer, lock_timeout=lock_timeout)


# name -> (function, description, parameters). Parameters follow JSON schema.
TOOLS = {
    "list_logs": (
        _list_logs,
        "List the active log files the agent manages, with size, inode and modification time.",
        {},
    ),
    "identify_log": (
        _identify_log,
        "Resolve a name, alias or description of a log to its validated path.",
        {"log": _LOG_ARG},
    ),
    "get_log_info": (
        _get_log_info,
        "Inode-level metadata of one log: size, disk usage, holes, permissions, owner, "
        "timestamps, and which processes have it open (and whether they use O_APPEND).",
        {"log": _LOG_ARG},
    ),
    "list_archives": (
        _list_archives,
        "List rotated archives, newest first, optionally only those of one log.",
        {"log": _LOG_ARG},
    ),
    "rotate_log": (
        _rotate_log,
        "Safely rotate a log that may still be written to: snapshot, gzip, verify, then empty "
        "it in place with ftruncate() so its inode and the writers' descriptors stay valid. "
        "Use dry_run first to see what would happen.",
        {
            "log": _LOG_ARG,
            "compress": {"type": "boolean", "description": "gzip the archive (default true)"},
            "truncate": {"type": "boolean",
                         "description": "empty the log after archiving (default true); "
                                        "false = archive only"},
            "dry_run": {"type": "boolean",
                        "description": "validate and report what would happen, change nothing"},
            "label": {"type": "string",
                      "description": "tag in the archive name, e.g. '2026-09' "
                                     "(letters, digits, '-' and '_')"},
            "watch_writer": {"type": "number",
                             "description": "seconds to watch the log grow again after rotation "
                                            "(default 0)"},
            "lock_timeout": {"type": "number",
                             "description": "seconds to wait if another rotation of this log "
                                            "is running (default 0 = fail immediately)"},
        },
    ),
}


def tool_specs() -> list:
    """Descriptions of all tools, in the format of an LLM function-calling API."""
    return [
        {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": params, "additionalProperties": False},
        }
        for name, (_, description, params) in TOOLS.items()
    ]


_TYPES = {
    "string": lambda v: isinstance(v, str),
    "boolean": lambda v: isinstance(v, bool),
    # bool is a subclass of int in Python, so True must not count as a number.
    # Infinity / NaN are rejected: watch_writer=inf could wait forever.
    "number": lambda v: (isinstance(v, (int, float)) and not isinstance(v, bool)
                         and math.isfinite(v) and v >= 0),
}


def _check_arguments(name: str, arguments, params: dict) -> None:
    if not isinstance(arguments, dict):
        raise RotatorError(errors.INVALID_REQUEST, f"Arguments for {name} must be a JSON object",
                           tool=name)
    unknown = sorted(set(arguments) - set(params))
    if unknown:
        raise RotatorError(errors.INVALID_REQUEST,
                           f"Unknown argument(s) for {name}: {', '.join(unknown)}",
                           tool=name, unknown_arguments=unknown, allowed_arguments=sorted(params))
    for arg, value in arguments.items():
        expected = params[arg]["type"]
        if value is not None and not _TYPES[expected](value):
            hint = "a non-negative number" if expected == "number" else f"a {expected}"
            raise RotatorError(errors.INVALID_REQUEST, f"Argument '{arg}' of {name} must be {hint}",
                               tool=name, argument=arg)


def call_tool(name: str, arguments: dict = None) -> dict:
    """Run one tool by name and return its structured result (never raises for expected errors)."""
    if arguments is None:
        arguments = {}
    try:
        if name not in TOOLS:
            raise RotatorError(errors.INVALID_REQUEST, f"Unknown tool: {name!r}",
                               tool=name, available_tools=list(TOOLS))
        func, _, params = TOOLS[name]
        _check_arguments(name, arguments, params)
        # None means "use the default", the same as leaving the argument out.
        return func(**{k: v for k, v in arguments.items() if v is not None})
    except RotatorError as err:
        return err.to_dict(action=name)
    except OSError as err:
        return errors.from_os_error(err).to_dict(action=name)
