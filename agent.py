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

    commands.add_parser("list", help="list the active logs").set_defaults(tool="list_logs", params=[])

    info = commands.add_parser("info", help="inode, size, permissions and open handles of a log")
    info.add_argument("log", nargs="?", help="log name, alias or path (default: apache_error.log)")
    info.set_defaults(tool="get_log_info", params=["log"])

    who = commands.add_parser("who", help="which processes have a log open (like lsof)")
    who.add_argument("log", nargs="?", help="log name, alias or path")
    who.set_defaults(tool="find_open_handles", params=["log"])

    rotate = commands.add_parser("rotate", help="archive a log and empty it in place")
    rotate.add_argument("log", nargs="?", help="log name, alias or path")
    rotate.add_argument("--dry-run", action="store_true", default=None,
                        help="only report what would happen")
    rotate.add_argument("--no-compress", dest="compress", action="store_const", const=False,
                        help="keep a plain copy instead of gzip")
    rotate.add_argument("--no-truncate", dest="truncate", action="store_const", const=False,
                        help="archive only, leave the log as it is")
    rotate.add_argument("--label", help="tag in the archive name, e.g. 2026-09")
    rotate.add_argument("--lock-timeout", type=float, metavar="SECONDS",
                        help="wait this long if another rotation is running")
    rotate.add_argument("--watch", dest="watch_writer", type=float, metavar="SECONDS",
                        help="watch the writer continue for up to SECONDS")
    rotate.set_defaults(tool="rotate_log", params=["log", "dry_run", "compress", "truncate", "label",
                                                   "lock_timeout", "watch_writer"])

    archives = commands.add_parser("archives", help="list rotated archives, newest first")
    archives.add_argument("log", nargs="?", help="only archives of this log")
    archives.set_defaults(tool="list_archives", params=["log"])

    commands.add_parser("tools", help="print the tool definitions (JSON schemas) for the master agent")
    return parser


def tool_call(args) -> tuple:
    """Turn parsed command-line arguments into (tool name, tool arguments)."""
    # Options the user did not give stay None and are left out, so the tool's defaults apply.
    return args.tool, {p: getattr(args, p) for p in args.params if getattr(args, p) is not None}


def format_result(result: dict) -> str:
    return json.dumps(result, indent=2)


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "tools":
        print(json.dumps(agent_tools.describe_tools(), indent=2))
        return 0
    name, arguments = tool_call(args)
    result = agent_tools.call_tool(name, arguments)
    print(json.dumps(result, indent=2) if args.json else format_result(result))
    return 0 if result["status"] == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
