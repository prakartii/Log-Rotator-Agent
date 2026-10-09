"""Natural-language requests -> one tool call.

    "rotate the apache error logs"
    "compress last month's nginx access log but keep the log"
    "what would happen if you rotated app.log?"
    "who is writing to the apache error log?"

The rules are simple keyword rules, not an LLM: the result must be
predictable, because a misunderstood request could truncate a log. Every
interpretation is returned with the request ("interpreted_as"), so the user
or the master agent can see exactly which tool ran with which arguments.

parse_request()  - text -> {"tool", "arguments", "explanation"}, changes nothing
handle_request() - parse_request() + call_tool()
"""

import re
from datetime import date, timedelta

from . import errors
from .errors import RotatorError


def _clean(text: str) -> str:
    """Lower-case, unify apostrophes, drop punctuation, collapse spaces."""
    text = text.lower().replace("’", "'").replace("‘", "'")
    text = re.sub(r"[?!,;]", " ", text)
    return " ".join(text.split())


# Option phrases. Each is removed from the text once recognised, so that what
# is left over names the log ("compress the nginx log without truncating").
_DRY_RUN = re.compile(r"\b(dry[ -]?run|what would happen|what happens|simulate|preview|pretend|"
                      r"without changing anything|don't change anything|do not change anything)\b")
_ARCHIVE_ONLY = re.compile(r"\b(archive only|only archive|back ?up|keep the log|keep it|"
                           r"(?:don't|do not|never|without) (?:truncat\w*|empty\w*|clear\w*))\b")
_NO_COMPRESS = re.compile(r"\b((?:don't|do not|without|no) (?:compress\w*|gzip\w*|zip\w*)|"
                          r"uncompressed|plain copy)\b")
# "make sure the writer keeps running" -> watch the log grow again after rotation.
_WATCH = re.compile(r"\b(?:make sure|check|verify|confirm|ensure)\b[a-z' ]*?\b(?:writer|process|app\w*|server)"
                    r"\b[a-z' ]*?\b(?:keeps?|still|continues?|running|writing)\w*\b"
                    r"(?: (?:running|writing|going|working))?")
_WATCH_SECONDS = 3.0
# "wait if another rotation is running" -> lock_timeout.
_WAIT = re.compile(r"\b(?:wait|queue)\b(?: (?:if|until|for)\b[a-z' ]*(?:busy|running|finish\w*|done|rotation))?")
_WAIT_SECONDS = 10.0

_MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august",
           "september", "october", "november", "december"]


def _previous_month(today: date) -> str:
    first = today.replace(day=1)
    return (first - timedelta(days=1)).strftime("%Y-%m")


def _find_label(text: str, today: date):
    """Return (label, matched phrase) for the archive name, or (None, None).

    "last month"      -> previous calendar month, e.g. "2026-09"
    "this month"      -> current month
    "for september"   -> the most recent September that has started
    "label nightly"   -> "nightly" (explicit labels)
    "2026-09"         -> "2026-09"
    The log itself is always rotated now; the label only names the archive.
    """
    match = re.search(r"\b(last|previous) month(?:'s)?\b", text)
    if match:
        return _previous_month(today), match.group(0)
    match = re.search(r"\bthis month(?:'s)?\b", text)
    if match:
        return today.strftime("%Y-%m"), match.group(0)
    match = re.search(r"\b(?:label(?:led)?|tag(?:ged)?|named?)(?: it)?(?: as)? ([a-z0-9_-]+)\b", text)
    if match:
        return match.group(1), match.group(0)
    match = re.search(r"\b(\d{4}-\d{2})(?:'s)?\b", text)
    if match:
        return match.group(1), match.group(0)
    match = re.search(r"\b(for |from |of )?(" + "|".join(_MONTHS) + r")(?:'s)?\b", text)
    if match and (match.group(2) != "may" or match.group(1)):  # "may I ..." is not a month
        month = _MONTHS.index(match.group(2)) + 1
        year = today.year if month <= today.month else today.year - 1
        return f"{year}-{month:02d}", match.group(0)
    return None, None


# What the user wants done. Checked in this order; the first match wins.
_REFUSE = re.compile(r"\b(delete|remove|rm|erase|unlink|destroy|wipe)\b")
_WHO = re.compile(r"\b(who|which process\w*|what process\w*|lsof|open by|opened by|holding|"
                  r"writing to|writes to|using)\b")
_LIST_ARCHIVES = re.compile(r"\b(archives|rotated (?:logs|files)|history|previous rotations|backups)\b")
_ROTATE = re.compile(r"\b(rotat(?:e|ion)|compress\w*|gzip\w*|truncat\w*|shrink\w*|empty|clean ?up|"
                     r"clear|free (?:up )?(?:disk )?space|archive|zip)\b")
_LIST_LOGS = re.compile(r"\b(list|which|what|show|available|all)\b")
_INFO = re.compile(r"\b(info\w*|details?|size|how (?:big|large)|inode|status|stat|describe|"
                   r"show|tell me about|look at|check)\b")

_STOP_WORDS = {
    "please", "can", "could", "would", "will", "you", "me", "i", "we", "us", "let's", "the", "a", "an",
    "my", "our", "this", "that", "these", "those", "of", "for", "to", "and", "but", "now", "it",
    "its", "is", "are", "be", "do", "does", "log", "logs", "file", "files", "in", "on", "at", "with",
    "then", "also", "just", "current", "currently", "active", "if", "from", "so", "give", "get",
    "about", "there", "here", "up", "out", "again", "today", "right", "away", "agent", "hey", "ok",
    "okay", "go", "ahead", "want", "need", "like", "some", "space", "disk", "rotated", "rotating",
    "else", "any", "how", "much", "many", "has", "have", "open", "opened", "let",
}


def _find_log(original: str, text: str, patterns) -> str:
    """What is left after removing intent and option words names the log (or None).

    A path ("logs/app.log", "/etc/passwd") is taken from the original text so its
    case is kept; it is still validated later by identify_log(), like any request.
    """
    for token in original.split():
        token = token.strip("'\"`?!,;:")
        if "/" in token:
            return token.rstrip(".")
    for pattern in patterns:
        text = pattern.sub(" ", text)
    words = []
    for word in text.split():
        word = word.strip(".\"`:")
        if word.endswith("'s"):  # "nginx's", or the "'s" left over from "what's"
            word = word[:-2]
        word = word.strip("'")
        if word and word not in _STOP_WORDS:
            words.append(word)
    return " ".join(words) or None


def parse_request(text: str, today: date = None) -> dict:
    """Interpret a request. Returns {"tool", "arguments", "explanation"}; runs nothing.

    Raises RotatorError(INVALID_REQUEST) for empty, unsafe or unclear requests.
    """
    if not isinstance(text, str) or not text.strip():
        raise RotatorError(errors.INVALID_REQUEST, "The request is empty", request=text)
    today = today or date.today()
    clean = _clean(text)

    if _REFUSE.search(clean):
        raise RotatorError(
            errors.INVALID_REQUEST,
            "The log rotator never deletes logs: a writer holding the deleted log would keep "
            "filling a file nobody can see. Ask it to rotate the log instead; that archives "
            "the contents and empties the log in place.",
            request=text, suggestion="rotate the log")

    label, label_phrase = _find_label(clean, today)
    used = [_DRY_RUN, _ARCHIVE_ONLY, _NO_COMPRESS, _WATCH, _WAIT, _WHO, _LIST_ARCHIVES, _ROTATE,
            _INFO, _LIST_LOGS]
    if label_phrase:
        used.append(re.compile(re.escape(label_phrase)))
    log = _find_log(text, clean, used)

    dry_run = bool(_DRY_RUN.search(clean))
    archive_only = bool(_ARCHIVE_ONLY.search(clean))
    no_compress = bool(_NO_COMPRESS.search(clean))
    if dry_run or archive_only or no_compress or _ROTATE.search(clean):
        tool, arguments = "rotate_log", _rotate_arguments(clean, log, label, dry_run, archive_only,
                                                          no_compress, text)
    elif _WHO.search(clean):
        tool, arguments = "find_open_handles", {"log": log} if log else {}
    elif _LIST_ARCHIVES.search(clean):
        tool, arguments = "list_archives", {"log": log} if log else {}
    elif log is None and _LIST_LOGS.search(clean) and re.search(r"\b(logs?|files?)\b", clean):
        tool, arguments = "list_logs", {}
    elif log is not None:
        tool, arguments = "get_log_info", {"log": log}  # "the apache log?" -> describe it
    else:
        raise RotatorError(
            errors.INVALID_REQUEST, f"Could not understand the request: {text!r}", request=text,
            examples=["rotate the apache error logs", "list the logs",
                      "what would happen if you rotated app.log", "who is writing to the nginx log",
                      "show the archives of the apache error log"])
    return {"tool": tool, "arguments": arguments, "explanation": _explain(tool, arguments)}


def _rotate_arguments(clean, log, label, dry_run, archive_only, no_compress, text) -> dict:
    if re.search(r"\b(all|every|everything)\b", clean) and log in (None, "all", "every", "everything"):
        raise RotatorError(errors.INVALID_REQUEST,
                           "Rotate one log at a time; name the log (see 'list the logs')",
                           needs_log=True,
                           request=text)
    if log is None:
        raise RotatorError(errors.INVALID_REQUEST, "Which log should be rotated? Name it, e.g. "
                           "'rotate the apache error log'", request=text, needs_log=True)
    arguments = {"log": log}
    if dry_run:
        arguments["dry_run"] = True
    if archive_only:
        arguments["truncate"] = False
    if no_compress:
        arguments["compress"] = False
    if label:
        arguments["label"] = label
    if _WATCH.search(clean):
        arguments["watch_writer"] = _WATCH_SECONDS
    if _WAIT.search(clean):
        arguments["lock_timeout"] = _WAIT_SECONDS
    return arguments


def _explain(tool: str, arguments: dict) -> str:
    """rotate_log(log='nginx access', label='2026-09') - shown to the user before/with the result."""
    return f"{tool}({', '.join(f'{k}={v!r}' for k, v in arguments.items())})"


def handle_request(text: str, log_dir=None, archive_dir=None, today: date = None) -> dict:
    """Interpret a request and run the matching tool. Never raises for expected errors.

    The result is the tool's own result plus "request" (the original text) and
    "interpreted_as" (tool, arguments, explanation), so it is always visible
    what the agent understood before it acted.
    """
    from .agent_tools import call_tool  # imported here: agent_tools does not depend on us

    try:
        parsed = parse_request(text, today=today)
    except RotatorError as err:
        result = err.to_dict(action="handle_request")
        if err.details.get("needs_log"):  # tell the user which logs there are
            listing = call_tool("list_logs", log_dir=log_dir)
            if listing["status"] == "success":
                result["available_logs"] = [log["name"] for log in listing["logs"]]
        return result
    result = call_tool(parsed["tool"], parsed["arguments"], log_dir=log_dir, archive_dir=archive_dir)
    return {**result, "request": text, "interpreted_as": parsed}
