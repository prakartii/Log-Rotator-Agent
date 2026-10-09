#!/usr/bin/env python3
"""Command-line interface of the Log Rotator Agent.

    python3 agent.py list                          # active logs
    python3 agent.py info "apache error"           # inode, size, open handles
    python3 agent.py rotate apache_error --dry-run # what would happen

Every command calls exactly one tool from log_rotator.agent_tools, the same
interface the master agent uses. --json prints the tool's result unchanged.
Exit status: 0 = success, 1 = the tool returned an error, 2 = bad command line.
"""

import argparse
import json
import sys

from log_rotator import agent_tools


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent.py", description="Log Rotator Agent")
    parser.add_argument("--json", action="store_true", help="print the raw JSON result")
    commands = parser.add_subparsers(dest="command", required=True, metavar="command")

    commands.add_parser("list", help="list the active logs").set_defaults(tool="list_logs")
    return parser


def tool_call(args) -> tuple:
    """Turn parsed command-line arguments into (tool name, tool arguments)."""
    return args.tool, {}


def format_result(result: dict) -> str:
    return json.dumps(result, indent=2)


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    name, arguments = tool_call(args)
    result = agent_tools.call_tool(name, arguments)
    print(json.dumps(result, indent=2) if args.json else format_result(result))
    return 0 if result["status"] == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
