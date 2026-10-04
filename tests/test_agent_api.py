"""The HTTP surface an Agent talks to. Every assertion runs against the real app + real tools."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from workflowhub.api import create_app
from workflowhub.bootstrap import build_hub

from test_discovery import fixtures

TOOL = "script-tools-report-tool"


class AgentApiTests(unittest.TestCase):
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

    def test_00_health_and_overview(self):
        health = self.client.get("/health").json()
        self.assertEqual(health["status"], "ok")
        self.assertGreaterEqual(health["tools"], 4)
        overview = self.client.get("/api/v1/overview").json()
        self.assertEqual(overview["catalog"]["total"], len(self.hub.catalog.tools))
        self.assertIn("recent_runs", overview)
        self.assertIn("approvals", overview)

    def test_01_agent_info_documents_the_surface(self):
        info = self.client.get("/api/v1/agent/info").json()
        for key in ("tools", "invoke", "schema", "guide", "mcp_config", "openapi"):
            self.assertIn(key, info["endpoints"], f"agent/info 少文档化端点 {key}")
        self.assertIn("dry_run", json.dumps(info["conventions"]))
        self.assertGreaterEqual(info["counts"]["catalogue"], 4)

    def test_02_tool_list_filters(self):
        listed = self.client.get("/api/v1/tools", params={"query": "report"}).json()
        self.assertIn(TOOL, [i["id"] for i in listed["items"]])
        by_kind = self.client.get("/api/v1/tools", params={"kind": "skill"}).json()
        self.assertTrue(all(i["kind"] == "skill" for i in by_kind["items"]))
        by_risk = self.client.get("/api/v1/tools", params={"risk": "high"}).json()
        self.assertTrue(all(i["risk"] == "high" for i in by_risk["items"]))
        self.assertEqual(self.client.get("/api/v1/tools", params={"query": "zzz"}).json()["total"], 0)

    def test_03_tool_detail_carries_contract_and_snippets(self):
        detail = self.client.get(f"/api/v1/tools/{TOOL}").json()
        self.assertEqual(detail["tool"]["id"], TOOL)
        self.assertIn("input", detail["skill"]["inputs"])
        self.assertIn("必填", detail["invocation"]["fields"]["input"]["description"])
        self.assertIn("input", detail["skill"]["metadata"]["required_args"])
        self.assertIn("/invoke", detail["invocation"]["endpoint"])
        for key in ("curl", "python", "function_calling", "mcp"):
            self.assertIn(key, detail["snippets"])
        self.assertIn(TOOL, detail["snippets"]["curl"])

    def test_04_dry_run_then_real_run(self):
        dry = self.client.post(f"/api/v1/tools/{TOOL}/invoke",
                               json={"inputs": {"flags": {"input": "report.md"}, "dry_run": True}}).json()
        self.assertEqual(dry["status"], "succeeded")
        self.assertIn("--input report.md", dry["outputs"]["command_line"])
        self.assertIsNone(dry["outputs"]["exit_code"])

        echo = self.client.post("/api/v1/tools", json={
            "name": "api echo", "command": [sys.executable, "-c", "print('api ok')"]}).json()
        real = self.client.post(f"/api/v1/tools/{echo['id']}/invoke",
                                json={"inputs": {}, "actor": "pytest"}).json()
        self.assertEqual(real["status"], "succeeded")
        self.assertIn("api ok", real["outputs"]["stdout"])
        self.assertEqual(real["tool"]["key"], echo["id"])
        self.assertTrue(real["artifacts"], "stdout 应该同时落成产物文件")

    def test_05_unknown_inputs_and_tools_are_structured_errors(self):
        bad = self.client.post(f"/api/v1/tools/{TOOL}/invoke", json={"inputs": {"nope": 1}})
        self.assertEqual(bad.status_code, 422)
        self.assertIn("未声明的输入", bad.json()["error"]["message"])
        self.assertEqual(bad.json()["error"]["details"]["unknown"], ["nope"])
        missing = self.client.post("/api/v1/tools/not-here/invoke", json={"inputs": {}})
        self.assertEqual(missing.status_code, 404)
        self.assertIn("未找到工具", missing.json()["error"]["message"])

    def test_06_disable_blocks_invocation(self):
        echo = self.client.post("/api/v1/tools", json={"name": "toggle me",
                                                       "command": [sys.executable, "-c", "print(1)"]}).json()
        self.client.patch(f"/api/v1/tools/{echo['id']}/state", json={"enabled": False})
        blocked = self.client.post(f"/api/v1/tools/{echo['id']}/invoke", json={"inputs": {}})
        self.assertEqual(blocked.status_code, 422)
        self.assertEqual(blocked.json()["error"]["code"], "permission_error")
        self.client.patch(f"/api/v1/tools/{echo['id']}/state", json={"enabled": True})
        self.assertEqual(self.client.post(f"/api/v1/tools/{echo['id']}/invoke",
                                          json={"inputs": {}}).json()["status"], "succeeded")

    def test_07_async_invoke_reports_task_then_result(self):
        echo = self.client.post("/api/v1/tools", json={"name": "async echo",
                                                       "command": [sys.executable, "-c", "print('later')"]}).json()
        started = self.client.post(f"/api/v1/tools/{echo['id']}/invoke",
                                   json={"inputs": {}, "wait": False}).json()
        self.assertIn("task_id", started)
        self.assertEqual(started["status"], "running")
        self.client.get(f"/api/v1/tasks/{started['task_id']}")  # must exist

    def test_08_approval_mode_gates_high_risk(self):
        risky = self.client.post("/api/v1/tools", json={"name": "gate", "risk": "high",
                                                        "command": ["C:\\x\\powershell.exe"]}).json()
        self.client.put("/api/v1/settings", json={"patch": {"approval_mode": True}})
        pending = self.client.post(f"/api/v1/tools/{risky['id']}/invoke", json={"inputs": {}}).json()
        self.assertEqual(pending["status"], "awaiting_approval")
        token = pending["approval"]["id"]
        self.assertIn(token, [a["id"] for a in self.client.get("/api/v1/approvals").json()["pending"]])
        decided = self.client.post(f"/api/v1/approvals/{token}/decide", json={"approve": True}).json()
        self.assertEqual(decided["status"], "approved")
        self.client.put("/api/v1/settings", json={"patch": {"approval_mode": False}})

    def test_09_schema_and_openapi(self):
        schema = self.client.get("/api/v1/tools.json").json()
        self.assertGreaterEqual(schema["count"], 2)
        expected = self.hub.catalog.to_skill(TOOL).id.replace("-", "_")
        names = [t["function"]["name"] for t in schema["tools"]]
        self.assertIn(expected, names)
        openapi = self.client.get("/openapi.json").json()
        self.assertIn("/api/v1/tools/{key}/invoke", openapi["paths"])

    def test_10_workflow_endpoints(self):
        workflow = json.loads((self.hub.roots["project"] / "examples" / "text_to_file.json")
                              .read_text(encoding="utf-8"))
        saved = self.client.post("/api/v1/workflows", json=workflow).json()
        self.assertEqual(saved["workflow_version_id"], "text-to-file:1")
        run = self.client.post("/api/v1/workflows/text-to-file/run",
                               json={"version": 1, "inputs": {"text": "api run"}}).json()
        self.assertEqual(run["status"], "succeeded")
        self.assertEqual(Path(run["outputs"]["file_path"]).read_text(encoding="utf-8"), "API RUN")
        listed = self.client.get("/api/v1/runs").json()["items"]
        self.assertIn("text-to-file", [r["workflow_id"] for r in listed])
        detail = self.client.get(f"/api/v1/runs/{run['id']}").json()
        self.assertEqual(len(detail["node_runs"]), 3)
        broken = {**workflow, "id": "broken", "version": 1, "edges": [],
                  "outputs": {}, "nodes": [{**workflow["nodes"][0],
                                            "skill_id": "nope", "skill_version": "9.9.9"}]}
        bad = self.client.post("/api/v1/workflows", json=broken)
        self.assertEqual(bad.status_code, 422)
        self.assertIn("unknown Skill", bad.json()["error"]["message"])

    def test_11_mixed_dag_chains_a_machine_tool_into_a_skill(self):
        skill_id = self.hub.catalog.to_skill(TOOL).id
        workflow = {
            "id": "mixed", "name": "命令输出接入手写 Skill", "version": 1,
            "inputs": {"text": {"type": "string", "required": True, "description": "内容"}},
            "outputs": {"file_path": {"type": "node_output", "node_id": "write", "field": "file_path"}},
            "nodes": [
                {"id": "probe", "type": "skill", "skill_id": skill_id, "skill_version": "1.0.0",
                 "inputs": {"argv": {"type": "literal", "value": ["--help"]},
                            "timeout_seconds": {"type": "literal", "value": 20}}},
                {"id": "write", "type": "skill", "skill_id": "file-writer", "skill_version": "1.0.0",
                 "inputs": {"text": {"type": "workflow_input", "name": "text"},
                            "filename": {"type": "literal", "value": "mixed.txt"}}},
            ],
            "edges": [{"source": "probe", "target": "write"}],
        }
        saved = self.client.post("/api/v1/workflows", json=workflow).json()
        self.assertTrue(saved.get("valid", True))
        run = self.client.post("/api/v1/workflows/mixed/run",
                               json={"version": 1, "inputs": {"text": "chained"}}).json()
        self.assertEqual(run["status"], "succeeded")
        self.assertEqual(Path(run["outputs"]["file_path"]).read_text(encoding="utf-8"), "chained")

    def test_12_file_reads_stay_inside_data_and_skills(self):
        outside = Path.home() / "secret.txt"
        outside.write_text("no", encoding="utf-8")
        try:
            denied = self.client.get("/api/v1/files", params={"path": str(outside)})
            self.assertEqual(denied.status_code, 422)
            self.assertEqual(denied.json()["error"]["code"], "permission_error")
        finally:
            outside.unlink(missing_ok=True)
        inside = self.hub.roots["data"] / "peek.json"
        inside.write_text('{"ok": true}', encoding="utf-8")
        allowed = self.client.get("/api/v1/files", params={"path": str(inside)}).json()
        self.assertIn('"ok"', allowed["text"])

    def test_13_window_bridge_is_inert_outside_desktop(self):
        state = self.client.get("/api/v1/window/state").json()
        self.assertFalse(state["desktop_mode"])
        acted = self.client.post("/api/v1/window/action", json={"action": "maximize"}).json()
        self.assertFalse(acted["ok"])
        self.assertIn("浏览器预览", acted["reason"])

    def test_14_settings_are_bounded(self):
        before = self.client.put("/api/v1/settings", json={"patch": {"max_output_bytes": 1, "default_timeout_seconds": 99999,
                                                                "not_a_setting": True}}).json()["settings"]
        self.assertGreaterEqual(before["max_output_bytes"], 2000)
        self.assertLessEqual(before["default_timeout_seconds"], 3600)
        self.assertNotIn("not_a_setting", before)

    def test_15_audit_records_every_invocation(self):
        activity = self.client.get("/api/v1/activity", params={"limit": 50}).json()["items"]
        self.assertTrue(any(row["type"] == "invoke" and row.get("actor") == "pytest" for row in activity))
        self.assertTrue(all("at" in row for row in activity))

    def test_16_ui_is_served(self):
        page = self.client.get("/")
        self.assertIn(200, (page.status_code,))
        if page.status_code == 200 and "text/html" in page.headers.get("content-type", ""):
            self.assertIn("WorkflowHub", page.text)

    def test_17_404_teaches_the_right_spelling(self):
        """Agent 会把 function-calling 的下划线名当 key；404 必须给出确切拼写而不是只说没有。"""
        misspelled = self.client.get(f"/api/v1/tools/{TOOL.replace('-', '_')}")
        self.assertEqual(misspelled.status_code, 404)
        self.assertIn(TOOL, misspelled.json()["error"]["message"])
        retry = self.client.post(f"/api/v1/tools/{TOOL}/invoke", json={"inputs": {"dry_run": True}})
        self.assertEqual(retry.status_code, 200, "按提示改回连字符就该能调")
        unknown = self.client.get("/api/v1/tools/definitely-not-here")
        self.assertIn("query=", unknown.json()["error"]["message"], "猜不出来的要指到搜索端点")


if __name__ == "__main__":
    unittest.main()
