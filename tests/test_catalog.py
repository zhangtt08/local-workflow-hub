"""Catalogue behaviour: persistence, enable/disable, risk gates, approvals, audit, Skill view."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from workflowhub.catalog import ToolCatalog
from workflowhub.errors import WorkflowHubError
from workflowhub.models import Permission, RiskLevel

from test_discovery import fixtures

TOOLS = {"script-tools-report-tool", "script-tools-wait-service"}


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.machines = self.root / "machine"
        self.machines.mkdir()
        fixtures(self.machines)
        self.catalog = ToolCatalog(self.root / "catalog")
        self.catalog.scan([str(self.machines)])

    def tearDown(self):
        self._tmp.cleanup()

    def test_scan_populates_and_persists(self):
        self.assertGreaterEqual(len(self.catalog.tools), 4)
        self.assertTrue(TOOLS <= set(self.catalog.tools))
        reloaded = ToolCatalog(self.root / "catalog")
        self.assertEqual(set(reloaded.tools), set(self.catalog.tools))

    def test_state_survives_rescan_and_only_custom_deletes(self):
        key = "script-tools-report-tool"
        self.catalog.set_flag(key, "starred", True)
        self.catalog.set_note(key, "每次跑周报都用它", alias="weekly-report")
        self.catalog.scan([str(self.machines)])
        row = self.catalog.record(key)
        self.assertTrue(row["starred"])
        self.assertEqual(row["notes"], "每次跑周报都用它")
        self.assertEqual(row["alias"], "weekly-report")
        with self.assertRaises(ValueError):
            self.catalog.remove(key)
        custom = self.catalog.add_custom({"name": "temp tool", "command": [sys.executable, "-c", "print(1)"]})
        self.catalog.remove(custom["id"])
        self.assertNotIn(custom["id"], self.catalog.tools)
        with self.assertRaises(ValueError):
            self.catalog.set_note(key, "x", alias="Bad Name")

    def test_skill_contract_is_executable_and_documented(self):
        skill = self.catalog.to_skill("script-tools-report-tool")
        self.assertEqual(skill.executor.type, "command")
        self.assertEqual(skill.executor.entry, sys.executable)
        self.assertTrue(Path(skill.executor.args[0]).is_absolute())
        self.assertEqual(skill.version, "1.0.0")
        # 解释器本身能执行任意代码，所以按高风险登记
        self.assertEqual(skill.risk_level, RiskLevel.HIGH)
        self.assertIn("argv", skill.inputs)
        self.assertFalse(skill.inputs["argv"].required)
        self.assertIn("input", skill.inputs)
        self.assertFalse(skill.inputs["input"].required)  # 命令自己校验必填，WorkflowHub 不拦 --help
        self.assertIn("必填", skill.inputs["input"].description)
        self.assertEqual(skill.metadata["required_args"], ["input"])
        self.assertIn(Permission.SHELL, skill.permissions)
        self.assertEqual(skill.metadata["catalog_key"], "script-tools-report-tool")

    def test_blueprints_are_never_invocable(self):
        blueprint = next(k for k, v in self.catalog.tools.items() if v["kind"] == "skill")
        self.assertFalse(self.catalog.is_enabled(blueprint))
        self.assertTrue(self.catalog.record(blueprint)["blueprint"])
        with self.assertRaises(WorkflowHubError):
            self.catalog.to_skill(blueprint)

    def test_high_risk_switch_gates_tools(self):
        custom = self.catalog.add_custom({"name": "shell helper", "command": ["C:\\Windows\\System32\\cmd.exe"],
                                          "risk": "high"})
        self.assertTrue(self.catalog.is_enabled(custom["id"]))
        self.catalog.update_settings({"high_risk_enabled": False})
        self.assertFalse(self.catalog.is_enabled(custom["id"]))
        row = self.catalog.record(custom["id"])
        self.assertFalse(row["enabled"])
        self.catalog.update_settings({"high_risk_enabled": True})
        self.assertTrue(self.catalog.is_enabled(custom["id"]))

    def test_approval_mode_lifecycle(self):
        key = self.catalog.add_custom({"name": "risky", "command": ["C:\\x\\powershell.exe"], "risk": "high"})["id"]
        self.assertFalse(self.catalog.needs_approval(key))
        self.catalog.update_settings({"approval_mode": True})
        self.assertTrue(self.catalog.needs_approval(key))
        approval = self.catalog.create_approval(key, {"argv": ["-Command", "Get-Process"]}, "agent-x")
        self.assertEqual(approval["status"], "pending")
        self.assertEqual(len(self.catalog.pending_approvals()), 1)
        decided = self.catalog.decide_approval(approval["id"], True)
        self.assertEqual(decided["status"], "approved")
        self.assertEqual(self.catalog.pending_approvals(), [])
        kinds = [json.loads(line)["type"] for line in
                 (self.root / "catalog" / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertIn("approval_requested", kinds)
        self.assertIn("approval_decided", kinds)

    def test_function_schema_shape(self):
        skill_id = self.catalog.to_skill("script-tools-report-tool").id
        schema = self.catalog.agent_tool_schema()
        self.assertTrue(schema)
        entry = next(item for item in schema if item["function"]["name"] == skill_id.replace("-", "_"))
        params = entry["function"]["parameters"]
        self.assertEqual(params["type"], "object")
        self.assertIn("必填", params["properties"]["input"]["description"])
        self.assertEqual(params["properties"]["argv"]["items"]["type"], "string")
        risk = self.catalog.record("script-tools-report-tool")["risk"]
        self.assertTrue(entry["function"]["description"].startswith(f"[{risk}]"))

    def test_facets_and_search_ranking(self):
        facets = self.catalog.facets()
        self.assertEqual(facets["total"], len(self.catalog.tools))
        self.assertLessEqual(facets["enabled"], facets["total"])
        page = self.catalog.list("report")
        self.assertGreaterEqual(page["total"], 1)
        self.assertEqual(page["items"][0]["id"], "script-tools-report-tool")
        self.assertEqual(page["facets"]["total"], facets["total"])
        self.assertEqual(self.catalog.list("zzz-not-a-tool")["total"], 0)

    def test_adopt_skill_package_copies_into_skills_root(self):
        blueprint = next(k for k, v in self.catalog.tools.items() if v["kind"] == "skill")
        skills_root = self.root / "skills-target"
        skills_root.mkdir()
        result = self.catalog.adopt_skill_package(blueprint, skills_root)
        self.assertTrue((skills_root / result["id"] / "skill.yaml").is_file())
        with self.assertRaises(WorkflowHubError):
            self.catalog.adopt_skill_package(blueprint, skills_root)

    def test_corrupt_catalog_does_not_take_the_app_down(self):
        path = self.root / "catalog" / "catalog.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        fresh = ToolCatalog(self.root / "catalog")
        self.assertEqual(fresh.tools, {})
        path.write_text(json.dumps({"schema_version": 1, "tools": {"x": {}}}), encoding="utf-8")
        stale = ToolCatalog(self.root / "catalog")
        self.assertEqual(stale.tools, {})
        self.assertTrue(stale.scan_errors)


class AuditCostTests(unittest.TestCase):
    """审计统计必须读一次文件就够：以前每条工具都重解析一遍，目录页与 /health 因此卡死。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.catalog = ToolCatalog(self.root / "catalog")
        for index in range(1200):
            self.catalog.tools[f"cli-tool-{index}"] = {
                "id": f"tool-{index}", "title": f"tool{index}.exe", "kind": "cli",
                "category": "命令行工具", "description": "", "tags": [], "risk": "low",
                "command": ["cmd.exe", "/c", "echo"], "cwd": str(self.root), "permissions": [],
                "args": [], "metadata": {},
            }
        self.audit = self.root / "catalog" / "audit.jsonl"
        self.audit.parent.mkdir(parents=True, exist_ok=True)
        self.audit.write_text("".join(
            json.dumps({"at": f"2026-10-03T00:00:{index % 60:02d}+00:00", "type": "invoke",
                        "tool": f"cli-tool-{index % 40}", "exit_code": 0}) + "\n"
            for index in range(400)), encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def _count_audit_reads(self, call):
        real = Path.read_text
        audit = self.audit
        reads = []

        def counted(this, *args, **kwargs):
            if this == audit:
                reads.append(1)
            return real(this, *args, **kwargs)

        with mock.patch.object(Path, "read_text", counted):
            result = call()
        return len(reads), result

    def test_facets_and_records_touch_the_audit_file_once(self):
        reads, facets = self._count_audit_reads(self.catalog.facets)
        self.assertEqual(reads, 0, "facets() 不该读审计")
        self.assertEqual(facets["total"], 1200)
        reads, listed = self._count_audit_reads(lambda: self.catalog.list(limit=5))
        self.assertLessEqual(reads, 1, f"目录页每条工具重读了一遍审计（读了 {reads} 次）")
        self.assertEqual(listed["total"], 1200)

    def test_index_matches_a_plain_scan_and_updates_after_a_write(self):
        _, before = self._count_audit_reads(self.catalog._audit_index)
        self.assertEqual(before["cli-tool-7"], (10, "2026-10-03T00:00:07+00:00"))
        self.assertEqual(sum(row[0] for row in before.values()), 400)
        self.catalog.audit({"type": "invoke", "tool": "cli-tool-7", "at": "2026-10-03T00:05:00+00:00"})
        after = self.catalog._audit_index()
        self.assertEqual(after["cli-tool-7"], (11, "2026-10-03T00:05:00+00:00"))
        self.assertEqual(self.catalog.invocation_count("cli-tool-7"), 11)
        self.assertEqual(self.catalog.last_invocation("cli-tool-7"), "2026-10-03T00:05:00+00:00")
        self.assertEqual(self.catalog._audit_rows(3)[0]["at"], "2026-10-03T00:05:00+00:00",
                         "recent_activity 仍要新→旧")


if __name__ == "__main__":
    unittest.main()
