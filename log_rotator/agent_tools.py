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
