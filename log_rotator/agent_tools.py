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

from . import errors
from .errors import RotatorError

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
