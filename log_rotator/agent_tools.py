"""The agent's tool interface: what the master agent (or the CLI) may call.

Every tool is registered with a name, a one-line description and a JSON
schema of its arguments. The master agent can read the list with
describe_tools() and then run a tool with call_tool(name, arguments).

call_tool() is the single entry point:

* it checks the tool name and the arguments against the schema,
* it never raises for expected problems, it always returns a dict with
  "status": "success" or "status": "error" plus a stable error_code,
* the LLM layer never touches files itself, it can only pick a tool.
"""

from pathlib import Path

from . import errors, rotate
from .errors import RotatorError
from .tools import log_info, processes

TOOLS = {}

_JSON_TYPES = {"string": str, "boolean": bool, "number": (int, float), "integer": int}


def tool(name: str, description: str, properties: dict = None, required=()):
    """Register a function as a tool the master agent may call."""
    def register(func):
        TOOLS[name] = {
            "name": name,
            "description": description,
            "input_schema": {
                "type": "object",
                "properties": properties or {},
                "required": list(required),
                "additionalProperties": False,
            },
            "function": func,
        }
        return func
    return register


def describe_tools() -> list:
    """Tool definitions as JSON-serialisable dicts (name, description, input_schema)."""
    return [{k: v for k, v in t.items() if k != "function"} for t in TOOLS.values()]


def _check_arguments(spec: dict, arguments) -> None:
    """Reject unknown, missing or wrongly typed arguments before anything runs."""
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise RotatorError(errors.INVALID_REQUEST, "Tool arguments must be a JSON object",
                           tool=spec["name"])
    schema = spec["input_schema"]
    unknown = sorted(set(arguments) - set(schema["properties"]))
    if unknown:
        raise RotatorError(errors.INVALID_REQUEST,
                           f"Unknown argument(s) for {spec['name']}: {', '.join(unknown)}",
                           tool=spec["name"], allowed=sorted(schema["properties"]))
    missing = [name for name in schema["required"] if name not in arguments]
    if missing:
        raise RotatorError(errors.INVALID_REQUEST,
                           f"Missing argument(s) for {spec['name']}: {', '.join(missing)}",
                           tool=spec["name"], missing=missing)
    for name, value in arguments.items():
        expected = schema["properties"][name]["type"]
        # bool is a subclass of int in Python, but true is not a number in JSON.
        if not isinstance(value, _JSON_TYPES[expected]) or (expected != "boolean" and isinstance(value, bool)):
            raise RotatorError(errors.INVALID_REQUEST,
                               f"Argument '{name}' of {spec['name']} must be a {expected}",
                               tool=spec["name"], argument=name)


def call_tool(name: str, arguments: dict = None, log_dir=None, archive_dir=None) -> dict:
    """Run one tool and return its structured result. Never raises for expected errors.

    log_dir / archive_dir override the configured directories (used by the
    CLI's --log-dir and by the tests); they are not tool arguments, so the
    master agent cannot point the agent at a different directory.
    """
    spec = TOOLS.get(name)
    if spec is None:
        return RotatorError(errors.INVALID_REQUEST, f"Unknown tool: {name!r}",
                            tool=name, available_tools=sorted(TOOLS)).to_dict(action="call_tool")
    try:
        _check_arguments(spec, arguments)
        context = {"log_dir": log_dir, "archive_dir": archive_dir}
        return spec["function"](context, **(arguments or {}))
    except RotatorError as err:
        return err.to_dict(action=name)
    except OSError as err:
        return errors.from_os_error(err).to_dict(action=name)


# ---------------------------------------------------------------------------
# The tools. Each receives the context (directories) first, then its arguments.

_LOG_ARG = {"type": "string",
            "description": "Name, alias, description or path of the log, e.g. 'the apache error logs'"}


def _roots(context):
    return [Path(context["log_dir"])] if context["log_dir"] else None


@tool("list_logs", "List the active log files that the agent manages, with size and inode.")
def _list_logs(context):
    return log_info.list_logs(context["log_dir"])


@tool("identify_log", "Resolve a log name, alias or description to the validated log path.",
      {"log": _LOG_ARG}, required=["log"])
def _identify_log(context, log):
    return log_info.identify_log(log, log_dir=context["log_dir"], allowed_roots=_roots(context))


@tool("get_log_info", "Inode, size, disk usage, permissions, owner, timestamps and open handles "
      "of one log.", {"log": _LOG_ARG})
def _get_log_info(context, log=None):
    path = _identify_log(context, log)["log"]
    return log_info.get_log_info(path, allowed_roots=_roots(context))


@tool("find_open_handles", "Which processes have the log open (like lsof): pid, command, fd, "
      "access mode, O_APPEND and offset.", {"log": _LOG_ARG})
def _find_open_handles(context, log=None):
    path = _identify_log(context, log)["log"]
    handles = processes.find_open_handles(path)
    return errors.success("find_open_handles", log=path, count=len(handles), open_by=handles,
                          warnings=[f"pid {h['pid']} writes without O_APPEND"
                                    for h in processes.unsafe_writers(handles)])


@tool("rotate_log", "Safely rotate a log: snapshot, gzip, verify, then empty it in place with "
      "ftruncate() so the writer keeps running. Use dry_run to only report what would happen.", {
          "log": _LOG_ARG,
          "compress": {"type": "boolean", "description": "gzip the archive (default true)"},
          "truncate": {"type": "boolean",
                       "description": "empty the log after archiving (default true); false = archive only"},
          "dry_run": {"type": "boolean", "description": "validate and report, change nothing"},
          "label": {"type": "string",
                    "description": "tag in the archive name, e.g. '2026-09' (letters, digits, - and _)"},
          "lock_timeout": {"type": "number",
                           "description": "seconds to wait if another rotation of this log is running"},
          "watch_writer": {"type": "number",
                           "description": "seconds to watch the writer continue after rotation"},
      })
def _rotate_log(context, log=None, **options):
    return rotate.rotate_log(log, archive_dir=context["archive_dir"], log_dir=context["log_dir"],
                             allowed_roots=_roots(context), **options)
