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

from log_rotator import agent_tools, language


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent.py", description="Log Rotator Agent")
    parser.add_argument("--json", action="store_true", help="print the raw JSON result")
    parser.add_argument("--log-dir", help="directory of the active logs (default: logs/)")
    parser.add_argument("--archive-dir", help="where archives are written (default: rotated_logs/)")
    commands = parser.add_subparsers(dest="command", required=True, metavar="command")

    ask = commands.add_parser("ask", help='plain-English request, e.g. "rotate the apache error logs"')
    ask.add_argument("request", nargs="+", help="the request (quotes are optional)")

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

    call = commands.add_parser("call", help="call any tool by name with JSON arguments")
    call.add_argument("tool", help="tool name, see the 'tools' command")
    call.add_argument("arguments", nargs="?", default="{}", help='JSON object, e.g. {"log": "app"}')
    return parser


def tool_call(args) -> tuple:
    """Turn parsed command-line arguments into (tool name, tool arguments)."""
    if args.command == "call":
        try:
            return args.tool, json.loads(args.arguments)
        except json.JSONDecodeError as err:
            raise ValueError(f"arguments are not valid JSON: {err}") from None
    # Options the user did not give stay None and are left out, so the tool's defaults apply.
    return args.tool, {p: getattr(args, p) for p in args.params if getattr(args, p) is not None}


def format_result(result: dict) -> str:
    """Human-readable text for a tool result; unknown results fall back to JSON."""
    if result["status"] == "error":
        return _format_error(result)
    formatter = _FORMATTERS.get(result.get("action"))
    return formatter(result) if formatter else json.dumps(result, indent=2)


def _format_error(result: dict) -> str:
    lines = [f"ERROR {result['error_code']}: {result['message']}"]
    if result.get("log_unchanged"):
        lines.append("The log was not changed.")
    elif result.get("truncated") and result.get("archive"):
        lines.append(f"The log WAS truncated; the old data is in {result['archive']}")
    for key in ("candidates", "available_logs", "available_tools"):
        if result.get(key):
            lines.append(f"{key.replace('_', ' ').capitalize()}: {', '.join(result[key])}")
    return "\n".join(lines)


_FORMATTERS = {}


def _formats(action):
    def register(func):
        _FORMATTERS[action] = func
        return func
    return register


@_formats("list_logs")
def _format_list(result):
    if not result["logs"]:
        return f"No logs in {result['log_dir']}"
    lines = [f"{result['count']} log(s) in {result['log_dir']}:"]
    for log in result["logs"]:
        lines.append(f"  {log['name']:<28} {log['size_human']:>10}   inode {log['inode']}   "
                     f"modified {log['modified']}")
    return "\n".join(lines)


def _format_handles(handles):
    if not handles:
        return ["  open by: no process"]
    return [f"  open by: pid {h['pid']} ({h['command']}) fd {h['fd']} {h['access']}"
            f"{' O_APPEND' if h['append'] else ' (no O_APPEND)'} offset {h['offset']}" for h in handles]


@_formats("get_log_info")
def _format_info(result):
    lines = [
        result["log"],
        f"  inode {result['inode']}   size {result['size_human']} ({result['size']} bytes)   "
        f"on disk {result['disk_usage']} bytes{'   SPARSE (has holes)' if result['sparse'] else ''}",
        f"  {result['mode']} {result['owner']}:{result['group']}   links {result['nlink']}   "
        f"modified {result['modified']}",
    ]
    lines += _format_handles(result.get("open_by", []))
    lines += [f"  WARNING: {w}" for w in result.get("warnings", [])]
    return "\n".join(lines)


@_formats("find_open_handles")
def _format_who(result):
    lines = [result["log"]] + _format_handles(result["open_by"])
    lines += [f"  WARNING: {w}" for w in result["warnings"]]
    return "\n".join(lines)


@_formats("list_archives")
def _format_archives(result):
    if not result["archives"]:
        return f"No archives in {result['archive_dir']}"
    lines = [f"{result['count']} archive(s) in {result['archive_dir']}, newest first:"]
    for archive in result["archives"]:
        label = f"  [{archive['label']}]" if archive["label"] else ""
        lines.append(f"  {archive['name']:<52} {archive['size_human']:>10}{label}")
    return "\n".join(lines)


@_formats("rotate")
def _format_rotate(result):
    if result["dry_run"]:
        lines = ["DRY RUN: nothing was changed.",
                 f"  log:        {result['log']} ({result['original_size_human']}, inode {result['inode_before']})",
                 f"  archive to: {result['would_archive_to']}",
                 f"  truncate:   {'yes, in place with ftruncate()' if result['would_truncate'] else 'no'}"]
    else:
        lines = [f"Rotated {result['log']}",
                 f"  archive:  {result['archive']} ({result['archive_size']} bytes, "
                 f"ratio {result['compression_ratio']})",
                 f"  archived: {result['bytes_archived']} bytes "
                 f"(caught up {result['caught_up_bytes']}, lost {result['bytes_lost']})"]
        if result["truncated"]:
            lines.append(f"  inode:    {result['inode_before']} -> {result['inode_after']} "
                         f"({'preserved' if result['inode_preserved'] else 'CHANGED'})")
        else:
            lines.append("  the log was not truncated (archive only)")
        if "writer_continues" in result:
            lines.append(f"  writer continues: {'yes' if result['writer_continues'] else 'not seen'}")
    lines += _format_handles(result.get("open_by", []))
    lines += [f"  WARNING: {w}" for w in result.get("warnings", [])]
    return "\n".join(lines)


@_formats("identify_log")
def _format_identify(result):
    return f"{result['query']!r} -> {result['log']} (matched by {result['matched_by']})"


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "tools":
        print(json.dumps(agent_tools.describe_tools(), indent=2))
        return 0
    if args.command == "ask":
        result = language.handle_request(" ".join(args.request), log_dir=args.log_dir,
                                         archive_dir=args.archive_dir)
    else:
        try:
            name, arguments = tool_call(args)
        except ValueError as err:
            parser.error(str(err))  # exits with status 2
        result = agent_tools.call_tool(name, arguments, log_dir=args.log_dir,
                                       archive_dir=args.archive_dir)
    print(json.dumps(result, indent=2) if args.json else format_result(result))
    return 0 if result["status"] == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
