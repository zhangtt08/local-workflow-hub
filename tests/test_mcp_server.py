"""MCP surface: the JSON-RPC handshake and tool calls, in-process and over a real stdio pipe."""

from __future__ import annotations

import asyncio
import json
import warnings
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from workflowhub.api import create_app  # noqa: F401  (import guard for the shared package)
from workflowhub.bootstrap import build_hub
from workflowhub.mcp_server import META_TOOLS, Handler, serve_stdio  # noqa: F401

from test_discovery import fixtures

ROOT = Path(__file__).resolve().parents[1]


class HandlerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name)
        (cls.root / "machine").mkdir()
        fixtures(cls.root / "machine")
        cls.hub = build_hub(cls.root / "data")
        cls.hub.catalog.scan([str(cls.root / "machine")])
        cls.hub.registry.refresh()
        cls.handler = Handler(cls.hub)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def call(self, name, arguments=None):
        return asyncio.run(self.handler.call(name, arguments or {}))

    def test_meta_tools_are_bounded(self):
        listed = self.handler.tools_list()
        self.assertEqual(listed["meta_tools"], len(META_TOOLS))
        self.assertEqual(len(listed["tools"]), len(META_TOOLS))
        names = {t["name"] for t in listed["tools"]}
        self.assertTrue({"search_tools", "get_tool", "invoke_tool", "run_workflow"} <= names)
        for tool in listed["tools"]:
            self.assertIn("inputSchema", tool)
            self.assertEqual(tool["inputSchema"]["type"], "object")
        self.assertIn("search_tools", listed["instructions"])

    def test_search_get_invoke_round_trip(self):
        found = self.call("search_tools", {"query": "report"})
        self.assertGreaterEqual(found["total"], 1)
        key = found["items"][0]["key"]
        detail = self.call("get_tool", {"key": key})
        self.assertEqual(detail["tool"]["id"], key)
        self.assertIn("argv", detail["fields"])
        dry = self.call("invoke_tool", {"key": key, "argv": ["--help"], "dry_run": True, "actor": "mcp-test"})
        self.assertEqual(dry["status"], "succeeded")
        self.assertTrue(dry["outputs"]["command_line"].endswith("--help"))

    def test_invoke_unknown_tool_raises(self):
        from workflowhub.errors import WorkflowHubError

        with self.assertRaises(WorkflowHubError):
            self.call("get_tool", {"key": "no-such-tool"})

    def test_workflows_and_activity(self):
        self.call("list_workflows")
        stats = self.call("catalog_stats")
        self.assertGreaterEqual(stats["total"], 4)
        self.assertIn("approval_mode", stats)
        self.assertIn("items", self.call("recent_activity", {"limit": 5}))


class StdioProtocolTests(unittest.TestCase):
    def test_initialize_and_tools_list_over_pipes(self):
        # Windows proactor pipes warn at GC time once communicate() has closed them
        warnings.filterwarnings("ignore", category=ResourceWarning)
        with tempfile.TemporaryDirectory() as folder:
            env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
                   "WORKFLOWHUB_DATA": str(Path(folder) / "data"),
                   "PYTHONPATH": str(ROOT / "backend")}
            process = subprocess.Popen([sys.executable, "-m", "workflowhub", "mcp"], stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, text=True,
                                       encoding="utf-8", cwd=str(ROOT / "backend"))
            requests = [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                            "clientInfo": {"name": "pytest", "version": "0"}}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                 "params": {"name": "catalog_stats", "arguments": {}}},
                {"jsonrpc": "2.0", "id": 4, "method": "unknown/method", "params": {}},
            ]
            try:
                stdout, stderr = process.communicate("\n".join(json.dumps(r) for r in requests) + "\n", timeout=120)
            except subprocess.TimeoutExpired:
                process.kill()
                stdout, stderr = process.communicate()
            lines = [json.loads(line) for line in stdout.splitlines() if line.strip()]
            by_id = {row.get("id"): row for row in lines}
            context = f"stdout={stdout[:400]!r} stderr={stderr[-600:]!r}"
            for stream in (process.stdout, process.stderr, process.stdin):
                if stream and not stream.closed:
                    stream.close()
            self.assertIn(1, by_id, f"initialize 没有回应：{context}")
            self.assertEqual(by_id[1]["result"]["protocolVersion"], "2024-11-05")
            self.assertEqual(by_id[1]["result"]["serverInfo"]["name"], "workflowhub")
            self.assertIn("tools", by_id[1]["result"]["capabilities"])
            self.assertGreaterEqual(len(by_id[2]["result"]["tools"]), 8)
            payload = json.loads(by_id[3]["result"]["content"][0]["text"])
            self.assertGreaterEqual(payload["total"], 1, f"stderr: {stderr[-400:]}")
            self.assertEqual(by_id[4]["error"]["code"], -32601)


if __name__ == "__main__":
    unittest.main()
