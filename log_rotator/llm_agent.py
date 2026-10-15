"""LLM tool calling: Claude reads the request and decides which tools to run.

    run_request("Zip up the apache error logs from last month and clear the current file.")

The tool definitions sent to Claude are exactly describe_tools() (name,
description, JSON schema). Claude answers with tool_use blocks; every one of
them goes through call_tool(), so the same schema checks and safety rules
apply as for the CLI. Claude never touches a file itself.

The loop: send the request -> run the tools Claude asked for -> send the
results back -> repeat until Claude stops calling tools (at most MAX_TOOL_CALLS
calls). Needs the `anthropic` package and an API key; available() says whether
both are there. Without them agent.py falls back to the keyword rules in
language.py.
"""

import json
import os
from datetime import date

from . import errors
from .agent_tools import call_tool, describe_tools, is_destructive
from .errors import RotatorError

MODEL = "claude-opus-5-5"
MAX_TOOL_CALLS = 8

SYSTEM_PROMPT = """You are the Log Rotator Agent of a Linux shell. You manage bloated log \
files without interrupting the processes that write to them, using only the tools you are given.

Rules:
- Do what the user asked with as few tool calls as possible. Do not ask for confirmation; the \
user already gave the command.
- rotate_log archives the WHOLE current log (snapshot, gzip, verify) and then empties it in place \
with ftruncate(), so the inode stays the same and the writer keeps running. "zip up", "compress", \
"archive", "clean up", "free space" and "clear the file" all mean rotate_log.
- "keep the log", "archive only", "don't clear it" -> truncate=false. "don't compress" -> \
compress=false. "what would happen", "dry run", "preview" -> dry_run=true.
- A period in the request ("last month", "september", "2026-09") does not select lines: \
rotate the whole log and pass the period as label in YYYY-MM form. Today is {today}.
- Never delete a log, and rotate one log at a time. If the user asks to delete, explain that \
rotating is the safe alternative instead of calling a tool.
- If you do not know which log is meant, call list_logs first.
- After the tools ran, answer in one or two short sentences: what was done and where the \
archive is. Report errors as they are; do not retry the same call."""


def available() -> bool:
    """True if the anthropic package is installed and credentials are configured."""
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def ask(text: str, log_dir=None, archive_dir=None) -> dict:
    """Entry point for the base shell: Claude if available(), else the keyword rules."""
    if available():
        return run_request(text, log_dir=log_dir, archive_dir=archive_dir)
    from .language import handle_request
    return {**handle_request(text, log_dir=log_dir, archive_dir=archive_dir), "engine": "rules"}


def _explain(name: str, arguments) -> str:
    if not isinstance(arguments, dict):  # call_tool() rejects it; just show it
        return f"{name}({arguments!r})"
    return f"{name}({', '.join(f'{k}={v!r}' for k, v in arguments.items())})"


def run_request(text: str, log_dir=None, archive_dir=None, client=None, today: date = None,
                confirm=None) -> dict:
    """Let Claude pick and run tools for a plain-English request. Never raises for expected errors.

    confirm(explanation) -> bool is asked before a call that empties a log
    (is_destructive); False skips the call and tells Claude the user declined.
    None = no confirmation (scripts, the master shell).

    The result is the last tool's own result (so the CLI can format it) plus
    "request", "engine": "llm", "tool_calls" (every call with its status),
    "interpreted_as" (the last call) and "reply" (Claude's final sentences).
    """
    calls = []
    last_result = None

    def finish(result, reply=""):
        out = {**result, "request": text, "engine": "llm", "tool_calls": calls, "reply": reply}
        if calls:
            out["interpreted_as"] = {k: calls[-1][k] for k in ("tool", "arguments", "explanation")}
        return out

    def fail(message):
        # If tools already ran (a log may already be rotated), their result must not be hidden.
        if last_result is not None:
            return finish(last_result, f"(Claude could not finish: {message})")
        return finish(RotatorError(errors.LLM_FAILED, message, request=text).to_dict(action="llm_request"))

    if not isinstance(text, str) or not text.strip():
        return RotatorError(errors.INVALID_REQUEST, "The request is empty",
                            request=text).to_dict(action="llm_request")
    if client is None:
        import anthropic
        client = anthropic.Anthropic()

    tools = describe_tools()
    system = SYSTEM_PROMPT.format(today=(today or date.today()).isoformat())
    messages = [{"role": "user", "content": text}]

    while True:
        try:
            response = client.beta.messages.create(
                model=MODEL,
                max_tokens=16000,
                system=system,
                tools=tools,
                messages=messages,
                output_config={"effort": "medium"},
                # On a safety decline the API retries on another model instead of failing.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except Exception as err:  # network, auth, rate limit: report it, never crash the shell
            return fail(f"Claude API request failed: {err}")

        if response.stop_reason == "refusal":
            return fail("Claude declined the request")
        if response.stop_reason == "max_tokens":
            return fail("Claude's answer was cut off")

        # The whole content goes back (thinking blocks included), as the API requires.
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason == "pause_turn":
            continue
        tool_uses = [block for block in response.content if block.type == "tool_use"]
        if not tool_uses:
            reply = " ".join(b.text for b in response.content if b.type == "text").strip()
            if last_result is None:  # answered without a tool, e.g. it refused to delete a log
                return finish(errors.success("llm_reply", message=reply), reply)
            return finish(last_result, reply)

        results = []
        for block in tool_uses:
            if len(calls) >= MAX_TOOL_CALLS:
                result = RotatorError(errors.INVALID_REQUEST,
                                      "Too many tool calls for one request").to_dict(action=block.name)
            else:
                explanation = _explain(block.name, block.input)
                if confirm and is_destructive(block.name, block.input) and not confirm(explanation):
                    result = RotatorError(errors.CANCELLED, "The user declined; the log was not changed",
                                          log_unchanged=True).to_dict(action=block.name)
                else:
                    result = call_tool(block.name, block.input, log_dir=log_dir, archive_dir=archive_dir)
                calls.append({"tool": block.name, "arguments": block.input,
                              "explanation": explanation,
                              "status": result["status"]})
                last_result = result
            results.append({"type": "tool_result", "tool_use_id": block.id,
                            "content": json.dumps(result), "is_error": result["status"] == "error"})
        # All results of one turn go back in a single user message.
        messages.append({"role": "user", "content": results})
        if len(calls) >= MAX_TOOL_CALLS:
            return finish(last_result, "Stopped after too many tool calls.")
