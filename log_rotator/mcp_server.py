"""MCP server: the agent's tools over the Model Context Protocol (stdio transport).

    python3 agent.py mcp                      # speaks MCP on stdin/stdout

An MCP client (the class master shell, Claude Desktop, Claude Code) starts this
process and talks JSON-RPC 2.0 to it, one JSON message per line. The server
exposes every entry of describe_tools() as an MCP tool; a tools/call request is
handed to call_tool(), so schema validation, the path allowlist and all other
guardrails apply exactly as they do for the CLI.

Implemented methods: initialize, ping, tools/list, tools/call (and the
notifications/initialized notification).
"""

import json
import sys

from . import __version__
from .agent_tools import call_tool, describe_tools

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "log-rotator"

# JSON-RPC 2.0 error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602


def list_tools() -> list:
    """describe_tools() in the MCP shape (inputSchema instead of input_schema)."""
    return [{"name": t["name"], "description": t["description"], "inputSchema": t["input_schema"]}
            for t in describe_tools()]


def _call(params, log_dir, archive_dir) -> dict:
    result = call_tool(params["name"], params.get("arguments") or {}, log_dir=log_dir,
                       archive_dir=archive_dir)
    return {"content": [{"type": "text", "text": json.dumps(result, indent=2)}],
            "structuredContent": result, "isError": result["status"] == "error"}


def handle_message(message, log_dir=None, archive_dir=None):
    """One JSON-RPC message in, the response out (None for notifications)."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or "method" not in message:
        return _error(message.get("id") if isinstance(message, dict) else None,
                      INVALID_REQUEST, "Not a JSON-RPC 2.0 request")
    method, params, request_id = message["method"], message.get("params") or {}, message.get("id")
    if request_id is None:  # notification: never answered
        return None
    if method == "initialize":
        result = {"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {"listChanged": False}},
                  "serverInfo": {"name": SERVER_NAME, "version": __version__}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": list_tools()}
    elif method == "tools/call":
        if not isinstance(params.get("name"), str):
            return _error(request_id, INVALID_PARAMS, "tools/call needs a tool name")
        result = _call(params, log_dir, archive_dir)
    else:
        return _error(request_id, METHOD_NOT_FOUND, f"Unknown method: {method}")
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def serve(stdin=None, stdout=None, log_dir=None, archive_dir=None) -> int:
    """Read one JSON message per line until end of input; write one response per request."""
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    for line in stdin:
        if not line.strip():
            continue
        try:
            response = handle_message(json.loads(line), log_dir, archive_dir)
        except json.JSONDecodeError:
            response = _error(None, PARSE_ERROR, "Invalid JSON")
        if response is not None:
            stdout.write(json.dumps(response) + "\n")
            stdout.flush()
    return 0
