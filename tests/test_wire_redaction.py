"""密钥在**线上那一份**必须遮掉 —— 这一组是"跑一次真工具再看响应字节"证出来的，不是读代码读的。

上一轮（3d2ee15）只把落盘那一道补齐了：`persistence.save()` 与 `catalog.audit()` 先脱敏再写。
出网那一道当时不存在，于是五条路一起漏：

1. `services.invoke_tool` 把执行现场那一份**原样**交给调用方（`outputs` + `logs`）；
2. `outputs["command_line"]` 是完整命令行（`command_executor.py` 拼的那一条）；
3. `logs[0]` 也是完整命令行，dry_run 那份是 `logs[0] = "dry_run: <命令行>"`；
4. `redaction.PASSTHROUGH_KEYS` 里躺着 `logs` —— 按名字豁免，等于整条日志不判；
5. 内存里那个 run 对象被 `POST /api/v1/workflows/{id}/run` 整个返回（存的 `inputs` 是原始现场）。

现在判据只有一个点：`api.py` 的中间件在响应出网前过 `redact_wire()`。逐个路由补是补不完的，
所以这一组用例测的是"**同一条 payload**，进程内是脏的、线上是干净的" —— 干净到没测过就等于没遮。

边界照旧写在 `redaction.py` 顶部：命令**自己**往 stdout 打一句与旗标无关的密钥，这一层无从判断。
所以靶子工具的 echo 用的是真实 CLI 的常见形状（把 argv 回显出来），这正是 `mask_echoes()` 认得出的那一类。
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

from local_client import local_client
from workflowhub import services
from workflowhub.bootstrap import build_hub
from workflowhub.redaction import MASK

#: 一把只在本文件里当"值"用的密钥。刻意不含 `--` 也不含 `-p`：那样它落在任何一段与旗标
#: 无关的文本里都**不会**被顺带遮掉，"响应里没有它"才是真的遮住了，而不是巧了。
SECRET = "sK9zTh4f1c8b7a65320Qx"
ENV_NAME = "FAKE_TOOL_SECRET"

#: 靶子工具：把 argv 原样回显（真实 CLI 的"我打算跑这条"形状），并只报 env 变量**名**在不在，
#: 永不打印它的值 —— 值进了 stdout 就是上面那条无从判断的边界，不在这个用例的射程里。
FAKE_TOOL = """\
import os, sys
print(" ".join(sys.argv))
print("env-present:" + ("yes" if os.environ.get("FAKE_TOOL_SECRET") else "no"))
sys.exit(0)
"""


def body_without_secret(text: str) -> bool:
    return SECRET not in text


class WireRedactionTests(unittest.TestCase):
    """一把带密钥的注册工具，从每一条会说到它的路上各读一遍。"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name)
        cls.tool_path = cls.root / "fake_tool.py"
        cls.tool_path.write_text(FAKE_TOOL, encoding="utf-8")
        cls.hub = build_hub(cls.root / "data")
        # 交付默认关着高风险（那是 BLOCKER 1 的另一半），这里测的是密钥会不会出去，
        # 所以按"用户已经打开开关"那一档走（tests/local_client.py 集中这一处）。
        cls.client = local_client(cls.hub)
        registered = cls.client.post("/api/v1/tools", json={
            "name": "带密钥的假工具", "command": [sys.executable, str(cls.tool_path)],
            "risk": "high", "description": "只用于脱敏回归"})
        assert registered.status_code == 200, registered.text      # 夹具里别用 cls.assertEqual
        cls.key = registered.json()["key"]
        cls.inputs = {"argv": ["--api-token", SECRET], "env": {ENV_NAME: SECRET},
                      "timeout_seconds": 60}
        invoked = cls.client.post(f"/api/v1/tools/{cls.key}/invoke",
                                  json={"inputs": cls.inputs, "actor": "redaction-test"})
        assert invoked.status_code == 200, invoked.text
        cls.invoke_body = invoked.json()
        cls.run_id = cls.invoke_body.get("run_id")
        # 真跑起来了才有资格谈脱敏：退出码 0 且 stdout 里有那行 env-present
        assert cls.invoke_body["status"] == "succeeded", invoked.text
        assert "env-present:yes" in cls.invoke_body["outputs"]["stdout"], invoked.text[:600]

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    # ── 反空转：同一条 payload 在进程内确实是脏的 ──────────────────────────────
    def test_the_same_payload_is_dirty_before_it_leaves_the_process(self):
        """`services.invoke_tool` 交出来的那一份确实带明文 —— 遮住它的确实是出口那一道。

        没有这一条，"响应里找不到密钥"就可能只是在密钥从没到达过的响应里数空气。
        """
        raw = asyncio.run(services.invoke_tool(self.hub, self.key, dict(self.inputs), "in-proc"))
        self.assertIn(SECRET, json.dumps(raw, ensure_ascii=False),
                      "执行现场那一份本该保持原样（跑命令用的就是它）")
        self.assertIn(SECRET, raw["outputs"]["command_line"])      # 泄漏路径 2
        self.assertIn(SECRET, raw["logs"][0])                      # 泄漏路径 3
        # 上面那条 asyncio.run 自己就走了一次中间件以外的路径；这里再按 HTTP 读同一条工具，
        # 确认线上那份是干净的。
        again = self.client.post(f"/api/v1/tools/{self.key}/invoke",
                                 json={"inputs": self.inputs, "actor": "redaction-test"})
        self.assertTrue(body_without_secret(again.text), again.text)

    def test_logs_is_no_longer_a_passthrough_key(self):
        """泄漏路径 4 的正身：`logs` 从豁免名单里拿掉了，并且拿掉这条是有牙的。"""
        from workflowhub.redaction import PASSTHROUGH_KEYS, redact_wire

        self.assertNotIn("logs", PASSTHROUGH_KEYS)
        dirty = {"logs": [f"C:\\x\\tool.py --api-token {SECRET}"]}
        self.assertNotIn(SECRET, json.dumps(redact_wire(dirty), ensure_ascii=False))
        # 旗标留着、值遮掉：审计要能看出"这次用了 --api-token"
        self.assertIn("--api-token", json.dumps(redact_wire(dirty), ensure_ascii=False))

    # ── invoke 自己 ────────────────────────────────────────────────────────────
    def test_invoke_response_has_no_secret_but_keeps_the_flag(self):
        response = self.client.post(f"/api/v1/tools/{self.key}/invoke",
                                    json={"inputs": self.inputs, "actor": "redaction-test"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(body_without_secret(response.text), response.text[:800])
        payload = response.json()
        self.assertIn("--api-token", payload["outputs"]["command_line"],
                      "遮的是值，不是旗标：命令行还得看得出用了哪面旗")
        self.assertIn(MASK, payload["outputs"]["command_line"])
        self.assertEqual(payload["outputs"]["exit_code"], 0, "工具必须真的跑完了")
        # stdout 里那行 argv 回声（`--api-token 值`）也得遮 —— 它按名字是豁免的，靠回声那一遍
        self.assertTrue(body_without_secret(payload["outputs"]["stdout"]), payload["outputs"]["stdout"])
        self.assertIn("env-present", payload["outputs"]["stdout"])

    def test_dry_run_logs_are_masked_too(self):
        """dry_run 不走子进程，但 `logs[0]` 是 `dry_run: <完整命令行>` —— 同一条泄漏路径。"""
        response = self.client.post(f"/api/v1/tools/{self.key}/invoke",
                                    json={"inputs": {**self.inputs, "dry_run": True},
                                          "actor": "redaction-test"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(body_without_secret(response.text), response.text[:800])
        self.assertIn("dry_run:", response.json()["logs"][0])

    # ── detail / history / list：凡是从现场读回来的都得干净 ─────────────────────
    def test_detail_history_and_list_endpoints_are_all_clean(self):
        surfaces = {
            "工具清单(list)": f"/api/v1/tools?query={self.key}",
            "工具详情(detail)": f"/api/v1/tools/{self.key}",
            "函数 schema(list)": "/api/v1/tools.json",
            "总览(list)": "/api/v1/overview",
            "搜索(list)": "/api/v1/search?q=fake_tool",
            "审计流水(history)": "/api/v1/activity?limit=200",
            "按工具查流水(history)": f"/api/v1/activity?tool={self.key}",
            "运行清单(history/list)": "/api/v1/runs?limit=50",
            "任务清单": "/api/v1/tasks?running_only=false",
            "待批审批": "/api/v1/approvals",
            "调用样例回显": f"/api/v1/agent/snippets/{self.key}",
        }
        if self.run_id:
            surfaces[f"运行详情(record)"] = f"/api/v1/runs/{self.run_id}"
        dirty = [name for name, url in surfaces.items()
                 if not body_without_secret(self.client.get(url).text)]
        self.assertEqual(dirty, [], f"这些端点还把明文密钥送出了网：{dirty}")

    def test_detail_and_history_still_show_the_call_happened(self):
        """遮值不遮事：详情与审计里仍然看得见这把工具被调过、用的哪面旗。"""
        detail = self.client.get(f"/api/v1/tools/{self.key}").json()
        self.assertTrue(any(row.get("type") == "invoke" for row in detail["audit"]), detail["audit"])
        listed = self.client.get("/api/v1/activity", params={"tool": self.key}).json()["items"]
        self.assertTrue(listed, "审计里应该有这次调用")
        self.assertTrue(any("--api-token" in json.dumps(row, ensure_ascii=False) for row in listed),
                        json.dumps(listed, ensure_ascii=False)[:600])
        self.assertTrue(all(body_without_secret(json.dumps(row, ensure_ascii=False)) for row in listed))

    def test_the_stored_run_record_on_disk_is_clean_too(self):
        """磁盘那一份也要有牙：`redact_run_record` 走的是节点 `inputs`/`outputs`，
        而节点 `result.logs` 里同样躺着完整命令行 —— 少遮一处，界面上"运行详情"读回来就是明文。"""
        self.assertTrue(self.run_id, "invoke 没带回 run_id，这条就没法验")
        record = next((json.loads(p.read_text(encoding="utf-8"))
                       for p in (self.hub.roots["data"] / "runs").glob("*.json")
                       if '"id"' in p.read_text(encoding="utf-8")
                       and json.loads(p.read_text(encoding="utf-8")).get("id") == self.run_id), None)
        self.assertIsNotNone(record, f"data/runs/ 里找不到 {self.run_id}")
        self.assertTrue(body_without_secret(json.dumps(record, ensure_ascii=False)),
                        json.dumps(record, ensure_ascii=False)[:900])
        # env 那一份在记录里也只剩变量名与遮码
        self.assertIn(ENV_NAME, json.dumps(record, ensure_ascii=False))

    # ── 内存里那个 run 对象（BLOCKER 2 的第 5 条路）────────────────────────────
    def test_in_memory_run_returned_by_workflow_run_is_clean(self):
        """`POST /api/v1/workflows/{id}/run` 返回的是**内存里**的 run，不是磁盘那份。

        它里面同时有：节点解析后的 `inputs`（argv 带密钥）、`definition_snapshot` 里
        按字面量绑进 argv 的那个值、以及节点 result 的 outputs/logs。这条路以前没人验过。
        """
        skill = self.hub.catalog.to_skill(self.key)
        workflow = {
            "id": "wire-secret-probe", "name": "把密钥按字面量绑进 argv", "version": 1,
            "inputs": {"text": {"type": "string", "required": True, "description": "内容"}},
            "outputs": {"file_path": {"type": "node_output", "node_id": "write", "field": "file_path"}},
            "nodes": [
                {"id": "probe", "type": "skill", "skill_id": skill.id, "skill_version": skill.version,
                 "inputs": {"argv": {"type": "literal", "value": ["--api-token", SECRET]},
                            "timeout_seconds": {"type": "literal", "value": 60}}},
                {"id": "write", "type": "skill", "skill_id": "file-writer", "skill_version": "1.0.0",
                 "inputs": {"text": {"type": "workflow_input", "name": "text"},
                            "filename": {"type": "literal", "value": "wire.txt"}}},
            ],
            "edges": [{"source": "probe", "target": "write"}],
        }
        saved = self.client.post("/api/v1/workflows", json=workflow)
        self.assertEqual(saved.status_code, 200, saved.text)
        run = self.client.post("/api/v1/workflows/wire-secret-probe/run",
                               json={"version": 1, "inputs": {"text": "chained"}})
        self.assertEqual(run.status_code, 200, run.text)
        self.assertEqual(run.json()["status"], "succeeded", run.text[:900])
        self.assertTrue(body_without_secret(run.text), run.text[:1200])
        self.assertIn("--api-token", run.text, "遮的是值，旗标与结构要留下")
        # 这份内存对象里的 argv 与定义快照都只剩遮码
        node_inputs = run.json()["node_runs"]["probe"]["inputs"]["argv"]
        self.assertIn(MASK, json.dumps(node_inputs, ensure_ascii=False))
        self.assertIn(MASK, json.dumps(run.json()["definition_snapshot"]["nodes"][0], ensure_ascii=False))

        # 同一条记录在**磁盘**上那份也得干净：`GET /api/v1/files?path=data/runs/<id>.json`
        # 把 JSON 原文按 `text` 这个豁免键交回来，而缩进过的数组里"旗标"与"值"隔着换行，
        # 命令行那条正则判不出来 —— 定义快照里的字面量密钥就是从这条路出去的。
        record_id = run.json()["id"]
        record_path = next((p for p in (self.hub.roots["data"] / "runs").glob("*.json")
                            if json.loads(p.read_text(encoding="utf-8")).get("id") == record_id), None)
        self.assertIsNotNone(record_path, f"data/runs/ 里没有 {record_id} 这份记录")
        self.assertTrue(body_without_secret(record_path.read_text(encoding="utf-8")),
                        record_path.read_text(encoding="utf-8")[:900])
        as_file = self.client.get("/api/v1/files", params={"path": str(record_path)})
        self.assertEqual(as_file.status_code, 200, as_file.text[:400])
        self.assertTrue(body_without_secret(as_file.text), as_file.text[:1200])
        # 结构没被遮坏：记录仍然按 WorkflowRun 读得回来，绑定类型也还在
        reread = self.client.get(f"/api/v1/runs/{record_id}")
        self.assertEqual(reread.status_code, 200, reread.text[:400])
        self.assertEqual(reread.json()["node_runs"]["probe"]["status"], "succeeded")

    # ── 审批面：payload 在进审批单之前就该遮 ───────────────────────────────────
    def test_approval_request_never_shows_the_secret(self):
        """审批单要给人看"这条命令是什么"，但值不该在他点头之前出去。"""
        with tempfile.TemporaryDirectory() as folder:
            hub = build_hub(Path(folder) / "data")
            client = local_client(hub, keep_defaults=True)
            registered = client.post("/api/v1/tools", json={
                "name": "带密钥的假工具", "command": [sys.executable, str(self.tool_path)],
                "risk": "high"})
            self.assertEqual(registered.status_code, 200, registered.text)
            key = registered.json()["key"]
            hub.catalog.update_settings({"high_risk_enabled": True, "approval_mode": True})
            hub.registry.refresh()
            pending = client.post(f"/api/v1/tools/{key}/invoke",
                                  json={"inputs": self.inputs, "actor": "approval-test"})
            self.assertEqual(pending.status_code, 200, pending.text)
            self.assertEqual(pending.json()["status"], "awaiting_approval", pending.text[:600])
            approvals = client.get("/api/v1/approvals")
            self.assertTrue(body_without_secret(approvals.text), approvals.text[:800])
            self.assertIn(key, approvals.text, "审批单还是要说清是哪把工具在等")


if __name__ == "__main__":
    unittest.main()
