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

from . import errors
from .errors import RotatorError


def _clean(text: str) -> str:
    """Lower-case, unify apostrophes and dashes, collapse spaces."""
    text = text.lower().replace("’", "'").replace("‘", "'")
    text = re.sub(r"[?!,;]", " ", text)
    return " ".join(text.split())
