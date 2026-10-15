#!/usr/bin/env python3
"""Command-line entry point of the Log Rotator Agent.

For people (readable output):

    python3 agent.py list                          # active logs
    python3 agent.py info the apache error logs    # inode, size, writers, warnings
    python3 agent.py rotate apache_error --dry-run # what would happen
    python3 agent.py rotate apache_error           # archive + gzip + ftruncate in place
    python3 agent.py archives                      # rotated archives, newest first

For the master agent (JSON in, JSON out):

    python3 agent.py tools                                      # tool descriptions
    python3 agent.py call rotate_log '{"log": "apache_error", "dry_run": true}'
    echo '{"log": "apache_error"}' | python3 agent.py call get_log_info -

Add --json to any command to get the raw result. Exit code: 0 = success,
1 = the tool returned an error, 2 = invalid command line.
"""

import argparse
import json
import sys

from log_rotator.interface import call_tool, tool_specs
from log_rotator.tools.log_info import human_size


def log_arg(words):
    """Join the LOG words, so `rotate the apache error logs` works without quotes."""
    return " ".join(words) or None


# ---------- readable output ----------

def print_error(result):
    print(f"error: {result['error_code']}: {result['message']}", file=sys.stderr)
    for key in ("candidates", "available_logs", "available_tools", "allowed_arguments"):
        if result.get(key):
            print(f"  {key.replace('_', ' ')}: {', '.join(map(str, result[key]))}", file=sys.stderr)
    if "truncated" in result:
        print(f"  log truncated: {result['truncated']}", file=sys.stderr)
    if result.get("truncated") and result.get("archive"):
        print(f"  archive (holds the old data): {result['archive']}", file=sys.stderr)


def print_table(rows, columns):
    """columns: (header, key) pairs."""
    widths = [max([len(h)] + [len(str(r[k])) for r in rows]) for h, k in columns]
    print("  ".join(h.ljust(w) for (h, _), w in zip(columns, widths)))
    for r in rows:
        print("  ".join(str(r[k]).ljust(w) for (_, k), w in zip(columns, widths)))


def print_handles(handles):
    for h in handles:
        append = "O_APPEND" if h.get("append") else "no O_APPEND"
        print(f"  pid {h['pid']:<8} fd {h['fd']:<4} {h['access']:<6} {append:<12} "
              f"offset {h['offset']}  {h['command']}")


def print_warnings(result):
    for warning in result.get("warnings", []):
        print(f"warning: {warning}")


def show_list(result):
    print(f"{result['count']} log(s) in {result['log_dir']}")
    if result["logs"]:
        print_table(result["logs"], [("NAME", "name"), ("SIZE", "size_human"), ("INODE", "inode"),
                                     ("MODIFIED", "modified")])


def show_identify(result):
    print(f"{result['log']}  (matched by {result['matched_by']})")


def show_info(result):
    fields = [
        ("log", result["log"]),
        ("inode", f"{result['inode']} (device {result['device']}, {result['nlink']} link(s))"),
        ("size", f"{result['size_human']} ({result['size']} bytes)"),
        ("disk usage", f"{human_size(result['disk_usage'])}" + ("  SPARSE (has holes)" if result["sparse"] else "")),
        ("mode", f"{result['mode']} ({result['mode_octal']})"),
        ("owner", f"{result['owner']}:{result['group']}"),
        ("modified", result["modified"]),
        ("access", ("r" if result["readable"] else "-") + ("w" if result["writable"] else "-")),
    ]
    for label, value in fields:
        print(f"{label + ':':<12}{value}")
    handles = result.get("open_by", [])
    print(f"open by:    {len(handles)} process(es)")
    print_handles(handles)
    print_warnings(result)


def show_archives(result):
    print(f"{result['count']} archive(s) in {result['archive_dir']}")
    if result["archives"]:
        print_table(result["archives"], [("NAME", "name"), ("SIZE", "size_human"),
                                         ("MODIFIED", "modified")])


def show_rotate(result):
    print(f"log:        {result['log']}")
    print(f"inode:      {result['inode_before']}   size: {result['original_size_human']}")
    if result.get("open_by"):
        print(f"open by:    {len(result['open_by'])} process(es)")
        print_handles(result["open_by"])
    if result["dry_run"]:
        print("\nDRY RUN - nothing was changed")
        print(f"  would archive to: {result['would_archive_to']}")
        print(f"  would truncate:   {result['would_truncate']}")
        print_warnings(result)
        return

    print("\nsteps:")
    for step in result["steps"]:
        print(f"  {step['step']:<16} {'ok' if step['ok'] else 'FAILED':<6} {step['ms']:>8.2f} ms")
    print(f"\narchive:    {result['archive']}")
    print(f"            {human_size(result['bytes_archived'])} -> {human_size(result['archive_size'])}"
          + (f" (ratio {result['compression_ratio']})" if result["compressed"] else " (uncompressed)"))
    if result["truncated"]:
        print(f"truncated:  yes, inode {result['inode_after']} "
              f"({'preserved' if result['inode_preserved'] else 'CHANGED'})")
        print(f"caught up:  {result['caught_up_bytes']} bytes written during rotation")
        print(f"lost:       {result['bytes_lost']} bytes")
        if "writer_continues" in result:
            print(f"writer:     {'still writing' if result['writer_continues'] else 'no new lines seen'}")
    else:
        print("truncated:  no (archive only)")
    print_warnings(result)


SHOW = {
    "list_logs": show_list,
    "identify_log": show_identify,
    "get_log_info": show_info,
    "list_archives": show_archives,
    "rotate_log": show_rotate,
}


# ---------- command line ----------

def build_parser():
    parser = argparse.ArgumentParser(
        prog="agent.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    # --json is accepted before or after the command.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="print the raw JSON result")
    parser.add_argument("--json", action="store_true", help="print the raw JSON result")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    p = sub.add_parser("list", parents=[common], help="list the active logs")
    p.add_argument("--period", help="only logs modified in this range, e.g. 'last month', 2026-09")

    for name, help_text in (("identify", "show which file a name or description refers to"),
                            ("info", "show inode-level details and writers of a log")):
        p = sub.add_parser(name, parents=[common], help=help_text)
        p.add_argument("log", nargs="*", metavar="LOG", help="log name, alias or description")

    p = sub.add_parser("archives", parents=[common], help="list rotated archives, newest first")
    p.add_argument("log", nargs="*", metavar="LOG", help="only archives of this log")
    p.add_argument("--period", help="only archives created in this range, e.g. 'last month'")

    p = sub.add_parser("rotate", parents=[common], help="archive and empty a log in place")
    p.add_argument("log", nargs="*", metavar="LOG", help="log name, alias or description")
    p.add_argument("--dry-run", action="store_true", help="report what would happen, change nothing")
    p.add_argument("--no-compress", action="store_true", help="keep a plain (uncompressed) archive")
    p.add_argument("--no-truncate", action="store_true", help="archive only, leave the log as it is")
    p.add_argument("--label", help="tag in the archive name, e.g. 2026-09")
    p.add_argument("--period", help="date range the contents belong to, e.g. 'last month'; "
                                    "tags the archive with it (2026-09)")
    p.add_argument("--watch", type=float, default=0.0, metavar="SECONDS",
                   help="watch the log grow again for up to SECONDS after rotation")
    p.add_argument("--lock-timeout", type=float, metavar="SECONDS",
                   help="wait if another rotation of this log is running")

    sub.add_parser("tools", help="print the tool descriptions for the master agent (JSON)")

    p = sub.add_parser("call", help="call a tool with JSON arguments, print the JSON result")
    p.add_argument("tool", help="tool name, see `agent.py tools`")
    p.add_argument("arguments", nargs="?", default="{}",
                   help="JSON object of arguments, or - to read it from stdin (default: {})")
    return parser


def request_from_args(args):
    """Turn the parsed command line into (tool name, arguments)."""
    if args.command == "list":
        return "list_logs", {"period": args.period}
    if args.command == "identify":
        return "identify_log", {"log": log_arg(args.log)}
    if args.command == "info":
        return "get_log_info", {"log": log_arg(args.log)}
    if args.command == "archives":
        return "list_archives", {"log": log_arg(args.log), "period": args.period}
    if args.command == "rotate":
        return "rotate_log", {
            "log": log_arg(args.log),
            "compress": not args.no_compress,
            "truncate": not args.no_truncate,
            "dry_run": args.dry_run,
            "label": args.label,
            "period": args.period,
            "watch_writer": args.watch,
            "lock_timeout": args.lock_timeout,
        }
    raise AssertionError(args.command)


def parse_call_arguments(text):
    """JSON arguments of `call`; returns (arguments, error result or None)."""
    if text == "-":
        text = sys.stdin.read()
    try:
        return json.loads(text or "{}"), None
    except json.JSONDecodeError as err:
        return None, {"status": "error", "error_code": "INVALID_REQUEST",
                      "message": f"Arguments are not valid JSON: {err}"}


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "tools":
        print(json.dumps(tool_specs(), indent=2))
        return 0

    if args.command == "call":
        arguments, error = parse_call_arguments(args.arguments)
        result = error or call_tool(args.tool, arguments)
        print(json.dumps(result, indent=2))  # `call` always answers in JSON
        return 0 if result["status"] == "success" else 1

    tool, arguments = request_from_args(args)
    result = call_tool(tool, arguments)
    if args.json:
        print(json.dumps(result, indent=2))
    elif result["status"] == "success":
        SHOW[tool](result)
    else:
        print_error(result)
    return 0 if result["status"] == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
