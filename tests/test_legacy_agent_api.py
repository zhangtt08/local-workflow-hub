"""The shared four-endpoint Agent contract (``/api/health`` · ``/api/agent/*``).

These tests pin the *legacy envelope* shape, because that is what the other ten projects' Agent
clients already parse. They also pin that the legacy face cannot smuggle past the catalogue's own
gates — disabling a tool or turning on approval mode must show up here as 403 / 409.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from workflowhub.api import create_app
from workflowhub.bootstrap import build_hub

from test_discovery import fixtures

TOOL = "script-tools-report-tool"


class LegacyAgentApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name)
        cls.machines = cls.root / "machine"
        cls.machines.mkdir()
        fixtures(cls.machines)
        cls.hub = build_hub(cls.root / "data")
        cls.hub.catalog.scan([str(cls.machines)])
        cls.hub.registry.refresh()
        cls.client = TestClient(create_app(cls.hub))

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_00_health_is_both_legacy_shapes(self):
        body = self.client.get("/api/health").json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["status"], "ok")
        for key in ("project", "version", "agent_api", "tools", "enabled", "uptime_ms", "gates"):
            self.assertIn(key, body["data"], f"/api/health 缺旧契约字段 {key}")
        self.assertEqual(body["data"]["tools"], len(self.hub.catalog.tools))

    def test_01_tools_descriptor_uses_shared_risk_vocabulary(self):
        listed = self.client.get("/api/agent/tools", params={"query": "report", "limit": 100}).json()
        self.assertTrue(listed["ok"])
        row = next((i for i in listed["data"] if i["name"] == TOOL), None)
        self.assertIsNotNone(row, "/api/agent/tools 的搜索没把脚本工具放进来")
        self.assertEqual(row["risk"], "destructive")
        self.assertFalse(row["input_schema"]["additionalProperties"])
        self.assertIn("argv", row["input_schema"]["properties"])
        self.assertIn("脚本要求必填", row["input_schema"]["properties"]["input"]["description"])

    def test_02_tools_list_pages_and_says_so(self):
        first = self.client.get("/api/agent/tools", params={"limit": 2}).json()
        self.assertEqual(first["returned"], len(first["data"]))
        self.assertGreater(first["count_total"], first["returned"], "本机目录应当超过一页")
        self.assertIn("offset", first["hint"])
        rest = self.client.get("/api/agent/tools", params={"limit": 2, "offset": 2}).json()
        self.assertNotEqual([i["name"] for i in first["data"]], [i["name"] for i in rest["data"]])
        capped = self.client.get("/api/agent/tools", params={"limit": 99999}).json()
        self.assertLessEqual(capped["returned"], 1000, "limit 必须有上限，否则一次能拖出几百 MB")

    def test_03_manifest_documents_the_native_faces(self):
        data = self.client.get("/api/agent/manifest").json()["data"]
        self.assertEqual(data["project"], "WorkflowHub")
        self.assertIn("catalogue", data["counts"])
        for key in ("health", "tools", "manifest", "tool", "native"):
            self.assertIn(key, data["endpoints"], f"manifest 未文档化 {key}")
        self.assertIn("failed_command", data["conventions"])

    def test_04_invoke_accepts_key_and_function_name_spellings(self):
        for name in (TOOL, TOOL.replace("-", "_"),
                     self.hub.catalog.to_skill(TOOL).id.replace("-", "_")):
            result = self.client.post("/api/agent/tool",
                                      json={"tool": name, "input": {"dry_run": True, "input": "hi"}})
            self.assertTrue(result.json()["ok"], f"{name} 没被解析到同一个工具：{result.json()}")
            self.assertEqual(result.json()["catalog_key"], TOOL)
            self.assertIn("command_line", result.json()["data"]["outputs"])

    def test_05_bad_requests_keep_the_legacy_error_envelope(self):
        unknown = self.client.post("/api/agent/tool", json={"tool": "no-such-tool"})
        self.assertEqual(unknown.status_code, 400)
        self.assertEqual(unknown.json()["error"]["code"], "unknown_tool")
        self.assertTrue(unknown.json()["available"])
        undeclared = self.client.post("/api/agent/tool", json={"tool": TOOL, "input": {"bogus": 1}})
        self.assertEqual(undeclared.status_code, 400)
        self.assertEqual(undeclared.json()["error"]["code"], "bad_input")
        self.assertEqual(undeclared.json()["workflowhub_code"], "input_error")
        broken = self.client.post("/api/agent/tool", content=b"{oops",
                                  headers={"content-type": "application/json"})
        self.assertEqual(broken.json()["error"]["code"], "bad_json")

    def test_06_gates_apply_on_the_legacy_face_too(self):
        self.hub.catalog.set_flag(TOOL, "enabled", False)
        try:
            denied = self.client.post("/api/agent/tool", json={"tool": TOOL, "input": {"dry_run": True}})
            self.assertEqual(denied.status_code, 403)
            self.assertEqual(denied.json()["error"]["code"], "permission_denied")
        finally:
            self.hub.catalog.set_flag(TOOL, "enabled", True)

        self.hub.catalog.update_settings({"approval_mode": True})
        try:
            held = self.client.post("/api/agent/tool", json={"tool": TOOL, "input": {"dry_run": True}})
            self.assertEqual(held.status_code, 409)
            self.assertEqual(held.json()["error"]["code"], "awaiting_approval")
        finally:
            self.hub.catalog.update_settings({"approval_mode": False})

    def test_07_two_faces_cannot_drift(self):
        """同一批断言必须两边同时成立：证明旧契约面不是另写了一套校验/执行/审计。"""
        call = {"argv": ["--version"], "dry_run": True}
        via_v1 = self.client.post(f"/api/v1/tools/{TOOL}/invoke",
                                  json={"inputs": call, "actor": "face-a"}).json()
        via_legacy = self.client.post("/api/agent/tool",
                                      json={"tool": TOOL, "input": call, "actor": "face-b"}).json()
        self.assertEqual(via_v1["status"], via_legacy["data"]["status"])
        self.assertEqual(via_v1["outputs"]["command_line"], via_legacy["data"]["outputs"]["command_line"])
        self.assertEqual(sorted(via_v1["outputs"]), sorted(via_legacy["data"]["outputs"]),
                         "两面拿到的输出字段必须一致")

        strict = self.client.post(f"/api/v1/tools/{TOOL}/invoke", json={"inputs": {"nope": 1}}).json()["error"]
        legacy_strict = self.client.post("/api/agent/tool", json={"tool": TOOL, "input": {"nope": 1}}).json()
        self.assertEqual(strict["message"], legacy_strict["error"]["message"])
        self.assertEqual(strict["code"], legacy_strict["workflowhub_code"])
        actors = {row.get("actor") for row in self.client.get("/api/v1/activity").json()["items"]}
        self.assertIn("face-b", actors, "旧契约面也必须落审计")


class LegacyTokenTests(unittest.TestCase):
    """The optional bearer token must cover the legacy face as well, or it is a bypass."""

    def setUp(self):
        os.environ["WORKFLOWHUB_TOKEN"] = "secret-token"
        self._tmp = tempfile.TemporaryDirectory()
        self.hub = build_hub(Path(self._tmp.name) / "data")
        self.client = TestClient(create_app(self.hub))

    def tearDown(self):
        del os.environ["WORKFLOWHUB_TOKEN"]
        self._tmp.cleanup()

    def test_08_token_guard_covers_the_legacy_face(self):
        self.assertEqual(
            self.client.get("/api/agent/tools", headers={"x-workflowhub-token": "nope"}).status_code, 401)
        self.assertEqual(self.client.get("/api/agent/tools").status_code, 401)
        self.assertTrue(self.client.get("/api/agent/tools",
                                        headers={"x-workflowhub-token": "secret-token"}).json()["ok"])
        self.assertEqual(self.client.get("/api/health").status_code, 401)


if __name__ == "__main__":
    unittest.main()
