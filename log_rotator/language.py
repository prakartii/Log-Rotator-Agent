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


# Option phrases. Each is removed from the text once recognised, so that what
# is left over names the log ("compress the nginx log without truncating").
_DRY_RUN = re.compile(r"\b(dry[ -]?run|what would happen|what happens|simulate|preview|pretend|"
                      r"without changing anything|don't change anything|do not change anything)\b")
_ARCHIVE_ONLY = re.compile(r"\b(archive only|only archive|back ?up|keep the log|keep it|"
                           r"(?:don't|do not|never|without) (?:truncat\w*|empty\w*|clear\w*))\b")
_NO_COMPRESS = re.compile(r"\b((?:don't|do not|without|no) (?:compress\w*|gzip\w*|zip\w*)|"
                          r"uncompressed|plain copy)\b")
