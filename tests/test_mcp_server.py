"""The MCP server: JSON-RPC over stdio, backed by call_tool()."""

import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from log_rotator import mcp_server
from log_rotator.agent_tools import describe_tools


def request(method, params=None, request_id=1):
    message = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


class McpServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.logs, self.archives = base / "logs", base / "rotated_logs"
        self.logs.mkdir()
        self.log = self.logs / "apache_error.log"
        self.data = b"[error] request failed\n" * 400
        self.log.write_bytes(self.data)

    def tearDown(self):
        self.tmp.cleanup()

    def handle(self, message):
        return mcp_server.handle_message(message, str(self.logs), str(self.archives))

    def test_initialize_announces_the_tools_capability(self):
        result = self.handle(request("initialize"))["result"]
        self.assertEqual(result["protocolVersion"], mcp_server.PROTOCOL_VERSION)
        self.assertIn("tools", result["capabilities"])
        self.assertEqual(result["serverInfo"]["name"], "log-rotator")

    def test_tools_list_matches_describe_tools(self):
        tools = self.handle(request("tools/list"))["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], [t["name"] for t in describe_tools()])
        rotate = next(t for t in tools if t["name"] == "rotate_log")
        self.assertEqual(rotate["inputSchema"]["type"], "object")
        self.assertIn("log", rotate["inputSchema"]["properties"])

    def test_tools_call_runs_the_tool(self):
        inode = os.stat(self.log).st_ino
        reply = self.handle(request("tools/call", {"name": "rotate_log", "arguments": {"log": "apache error"}}))
        result = reply["result"]
        self.assertFalse(result["isError"], result)
        self.assertEqual(result["structuredContent"]["action"], "rotate")
        self.assertEqual(json.loads(result["content"][0]["text"])["status"], "success")
        self.assertEqual(os.stat(self.log).st_size, 0)
        self.assertEqual(os.stat(self.log).st_ino, inode)

    def test_tool_errors_are_flagged_and_change_nothing(self):
        reply = self.handle(request("tools/call", {"name": "rotate_log", "arguments": {"log": "/etc/passwd"}}))
        self.assertTrue(reply["result"]["isError"])
        self.assertEqual(reply["result"]["structuredContent"]["error_code"], "OUTSIDE_ALLOWED_DIR")
        self.assertEqual(self.log.read_bytes(), self.data)

    def test_schema_validation_applies_to_mcp_calls(self):
        reply = self.handle(request("tools/call", {"name": "rotate_log",
                                                   "arguments": {"log": "apache", "truncate": "yes"}}))
        self.assertTrue(reply["result"]["isError"])
        self.assertEqual(self.log.read_bytes(), self.data)

    def test_unknown_tool_is_a_tool_error(self):
        reply = self.handle(request("tools/call", {"name": "delete_log", "arguments": {}}))
        self.assertTrue(reply["result"]["isError"])

    def test_protocol_errors(self):
        self.assertEqual(self.handle(request("no/such/method"))["error"]["code"], mcp_server.METHOD_NOT_FOUND)
        self.assertEqual(self.handle(request("tools/call", {}))["error"]["code"], mcp_server.INVALID_PARAMS)
        self.assertEqual(self.handle({"id": 1})["error"]["code"], mcp_server.INVALID_REQUEST)

    def test_notifications_get_no_answer(self):
        self.assertIsNone(self.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_serve_reads_lines_and_writes_one_response_each(self):
        lines = [json.dumps(request("initialize", request_id=1)),
                 json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
                 "",
                 "not json",
                 json.dumps(request("tools/call", {"name": "list_logs"}, request_id=2))]
        out = io.StringIO()
        status = mcp_server.serve(io.StringIO("\n".join(lines) + "\n"), out, str(self.logs), str(self.archives))
        replies = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual(status, 0)
        self.assertEqual([r["id"] for r in replies], [1, None, 2])
        self.assertEqual(replies[1]["error"]["code"], mcp_server.PARSE_ERROR)
        self.assertEqual(replies[2]["result"]["structuredContent"]["count"], 1)


if __name__ == "__main__":
    unittest.main()
