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

    def test_refusal(self):
        result = self.run_llm(FakeClient(response(stop_reason="refusal")))
        self.assertEqual(result["error_code"], errors.LLM_FAILED)

    def test_number_of_tool_calls_is_limited(self):
        client = FakeClient(*[response(tool_use("list_logs", {}, f"toolu_{i}"))
                              for i in range(llm_agent.MAX_TOOL_CALLS)])
        result = self.run_llm(client, "list the logs forever")
        self.assertEqual(len(result["tool_calls"]), llm_agent.MAX_TOOL_CALLS)
        self.assertIn("too many tool calls", result["reply"])

    def test_empty_request(self):
        result = self.run_llm(FakeClient(), "  ")
        self.assertEqual(result["error_code"], errors.INVALID_REQUEST)


class CliEngineTest(LlmTestCase):
    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = agent.main(["--log-dir", str(self.logs), "--archive-dir", str(self.archives), *argv])
        return status, out.getvalue()

    def test_ask_with_llm_engine_shows_the_calls_and_the_reply(self):
        client = FakeClient(response(tool_use("rotate_log", {"log": "apache error", "label": "2026-09"})),
                            response(text("Done.")))
        with mock.patch.object(llm_agent, "available", return_value=True), \
                mock.patch.object(llm_agent, "run_request",
                                  functools.partial(llm_agent.run_request, client=client)):
            status, out = self.run_cli("ask", EXAMPLE)
        self.assertEqual(status, 0, out)
        self.assertTrue(out.startswith(
            "Claude called: rotate_log(log='apache error', label='2026-09') -> success\n"), out)
        self.assertIn("Claude: Done.", out)

    def test_auto_falls_back_to_the_keyword_rules(self):
        with mock.patch.object(llm_agent, "available", return_value=False):
            status, out = self.run_cli("ask", EXAMPLE)
        self.assertEqual(status, 0, out)
        self.assertTrue(out.startswith("Understood: rotate_log(log='apache error', label="), out)

    def test_llm_engine_without_api_key_is_a_usage_error(self):
        with mock.patch.object(llm_agent, "available", return_value=False), \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exit_:
            self.run_cli("ask", "--engine", "llm", EXAMPLE)
        self.assertEqual(exit_.exception.code, 2)


class AvailableTest(unittest.TestCase):
    def test_not_available_without_the_anthropic_package(self):
        with mock.patch.dict(sys.modules, {"anthropic": None}), \
                mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "key"}):
            self.assertFalse(llm_agent.available())

    def test_not_available_without_credentials(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("ANTHROPIC_")}
        with mock.patch.dict(sys.modules, {"anthropic": SimpleNamespace()}), \
                mock.patch.dict(os.environ, env, clear=True):
            self.assertFalse(llm_agent.available())

    def test_available_with_package_and_key(self):
        with mock.patch.dict(sys.modules, {"anthropic": SimpleNamespace()}), \
                mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "key"}):
            self.assertTrue(llm_agent.available())


class AskTest(LlmTestCase):
    def test_ask_uses_the_keyword_rules_without_claude(self):
        with mock.patch.object(llm_agent, "available", return_value=False):
            result = llm_agent.ask("rotate the apache error logs", log_dir=str(self.logs),
                                   archive_dir=str(self.archives))
        self.assertEqual(result["engine"], "rules")
        self.assertEqual(result["action"], "rotate")
        self.assertEqual(os.stat(self.log).st_size, 0)

    def test_ask_uses_claude_when_available(self):
        client = FakeClient(response(tool_use("list_logs", {})), response(text("One log.")))
        with mock.patch.object(llm_agent, "available", return_value=True), \
                mock.patch.object(llm_agent, "run_request",
                                  functools.partial(llm_agent.run_request, client=client)):
            result = llm_agent.ask("which logs are there?", log_dir=str(self.logs))
        self.assertEqual(result["engine"], "llm")
        self.assertEqual(result["reply"], "One log.")


if __name__ == "__main__":
    unittest.main()
