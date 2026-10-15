"""The Claude tool-calling loop, tested with a scripted fake client (no network, no API key)."""

import contextlib
import functools
import gzip
import io
import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from log_rotator import errors, llm_agent
from log_rotator.agent_tools import describe_tools

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import agent  # noqa: E402

EXAMPLE = "Zip up the apache error logs from last month and clear the current file."


def tool_use(name, arguments, block_id="toolu_1"):
    return SimpleNamespace(type="tool_use", id=block_id, name=name, input=arguments)


def text(value):
    return SimpleNamespace(type="text", text=value)


def response(*content, stop_reason=None):
    if stop_reason is None:
        stop_reason = "tool_use" if any(b.type == "tool_use" for b in content) else "end_turn"
    return SimpleNamespace(content=list(content), stop_reason=stop_reason)


class FakeClient:
    """Plays back scripted responses (or raises scripted exceptions) and records every request."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class LlmTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.logs = base / "logs"
        self.archives = base / "rotated_logs"
        self.logs.mkdir()
        self.log = self.logs / "apache_error.log"
        self.data = b"".join(f"[error] request {i} failed\n".encode() for i in range(500))
        self.log.write_bytes(self.data)

    def tearDown(self):
        self.tmp.cleanup()

    def run_llm(self, client, request=EXAMPLE):
        return llm_agent.run_request(request, log_dir=str(self.logs), archive_dir=str(self.archives),
                                     client=client, today=date(2026, 10, 15))


class RunRequestTest(LlmTestCase):
    def test_example_prompt_rotates_through_the_tool_claude_picked(self):
        inode = os.stat(self.log).st_ino
        client = FakeClient(
            response(tool_use("rotate_log", {"log": "apache error", "label": "2026-09"})),
            response(text("Archived the apache error log and emptied it.")),
        )
        result = self.run_llm(client)

        self.assertEqual(result["status"], "success", result)
        self.assertEqual(result["engine"], "llm")
        self.assertEqual(result["reply"], "Archived the apache error log and emptied it.")
        self.assertEqual(result["interpreted_as"]["explanation"],
                         "rotate_log(log='apache error', label='2026-09')")
        # The real rotation happened: archive written, log emptied in place.
        self.assertEqual(os.stat(self.log).st_size, 0)
        self.assertEqual(os.stat(self.log).st_ino, inode)
        self.assertIn(".2026-09.", result["archive"])
        with gzip.open(result["archive"], "rb") as f:
            self.assertEqual(f.read(), self.data)


if __name__ == "__main__":
    unittest.main()
