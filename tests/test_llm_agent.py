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

    def test_claude_gets_the_tool_schemas_and_the_results(self):
        client = FakeClient(response(tool_use("list_logs", {})), response(text("One log.")))
        self.run_llm(client, "which logs are there?")

        first, second = client.requests
        self.assertEqual(first["tools"], describe_tools())
        self.assertEqual(first["model"], llm_agent.MODEL)
        self.assertIn("Today is 2026-10-15", first["system"])
        self.assertEqual(first["messages"], [{"role": "user", "content": "which logs are there?"}])
        tool_result = second["messages"][-1]["content"][0]
        self.assertEqual(tool_result["tool_use_id"], "toolu_1")
        self.assertFalse(tool_result["is_error"])
        self.assertIn("apache_error.log", tool_result["content"])

    def test_tool_errors_go_back_to_claude_and_change_nothing(self):
        client = FakeClient(response(tool_use("rotate_log", {"log": "/etc/passwd"})),
                            response(text("That file is outside the log directory.")))
        result = self.run_llm(client, "rotate /etc/passwd")

        self.assertEqual(result["error_code"], errors.OUTSIDE_ALLOWED_DIR)
        self.assertTrue(client.requests[1]["messages"][-1]["content"][0]["is_error"])
        self.assertEqual(self.log.read_bytes(), self.data)

    def test_invalid_arguments_are_rejected_by_the_schema_check(self):
        client = FakeClient(response(tool_use("rotate_log", {"log": "apache", "truncate": "yes"})),
                            response(text("Sorry.")))
        result = self.run_llm(client)
        self.assertEqual(result["error_code"], errors.INVALID_REQUEST)
        self.assertEqual(self.log.read_bytes(), self.data)

    def test_answer_without_a_tool(self):
        client = FakeClient(response(text("I never delete logs; I can rotate it instead.")))
        result = self.run_llm(client, "delete the apache log")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["action"], "llm_reply")
        self.assertEqual(result["tool_calls"], [])
        self.assertEqual(self.log.read_bytes(), self.data)

    def test_api_failure_before_any_tool(self):
        result = self.run_llm(FakeClient(ConnectionError("no network")))
        self.assertEqual(result["error_code"], errors.LLM_FAILED)
        self.assertEqual(self.log.read_bytes(), self.data)

    def test_api_failure_after_a_rotation_still_reports_the_rotation(self):
        client = FakeClient(response(tool_use("rotate_log", {"log": "apache error"})),
                            ConnectionError("no network"))
        result = self.run_llm(client)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["action"], "rotate")
        self.assertIn("could not finish", result["reply"])


if __name__ == "__main__":
    unittest.main()
