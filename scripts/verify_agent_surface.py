"""真机端到端检查：扫描本机 → 搜索 → dry_run → 真实调用 → 审批 → DAG → MCP。

在仓库根执行：python scripts/verify_agent_surface.py
与 tests/（用临时目录、只读夹具）互补：这里验证的是这台电脑上的真实工具面。
"""
import asyncio
import json
import sys
import time
from pathlib import Path


from fastapi.testclient import TestClient  # noqa: E402

from workflowhub.api import create_app  # noqa: E402
from workflowhub.bootstrap import build_hub  # noqa: E402
from workflowhub import services  # noqa: E402
from workflowhub.mcp_server import Handler  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
out = []


def check(label, ok, detail=""):
    out.append({"step": label, "ok": bool(ok), "detail": str(detail)[:300]})
    print(("PASS " if ok else "FAIL ") + label + " :: " + str(detail)[:220])


hub = build_hub()
t0 = time.time()
scan = hub.catalog.scan()
hub.registry.refresh()
check("scan", scan["count"] > 100, f"{scan} in {time.time()-t0:.1f}s")

client = TestClient(create_app(hub))
h = client.get("/health").json()
check("health", h["status"] == "ok", h)

rows = client.get("/api/v1/tools", params={"query": "curl", "limit": 5}).json()
check("list-tools", rows["total"] > 0, {"total": rows["total"], "keys": [i["id"] for i in rows["items"]]})

target = "cli-python" if "cli-python" in hub.catalog.tools else "sys-curl"
detail = client.get(f"/api/v1/tools/{target}").json()
check("tool-detail", "skill" in detail and detail["tool"]["id"] == target,
      {"key": target, "fields": sorted(detail["skill"]["inputs"]), "cmd": detail["tool"]["command"]})

dry = client.post(f"/api/v1/tools/{target}/invoke", json={"inputs": {"argv": ["--version"], "dry_run": True}}).json()
check("dry-run", dry.get("status") == "succeeded" and "--version" in str(dry.get("outputs", {}).get("command_line", "")), dry)

real = client.post(f"/api/v1/tools/{target}/invoke", json={"inputs": {"argv": ["--version"], "timeout_seconds": 30}, "actor": "smoke"}).json()
check("real-invoke", real.get("status") == "succeeded" and (real.get("outputs", {}).get("stdout") or real.get("outputs", {}).get("stderr")),
      {"status": real.get("status"), "exit": real.get("outputs", {}).get("exit_code"),
       "out": (real.get("outputs", {}).get("stdout") or real.get("outputs", {}).get("stderr"))[:120]})

bad = client.post(f"/api/v1/tools/{target}/invoke", json={"inputs": {"nope": "x"}})
check("unknown-input-422", bad.status_code == 422, bad.json())

missing = client.post("/api/v1/tools/does-not-exist/invoke", json={"inputs": {}})
check("unknown-tool-404", missing.status_code == 404, missing.json())

state = client.patch(f"/api/v1/tools/{target}/state", json={"starred": True}).json()
check("star", state.get("starred") is True, state.get("starred"))
off = client.patch(f"/api/v1/tools/{target}/state", json={"enabled": False}).json()
hub.registry.refresh()
blocked = client.post(f"/api/v1/tools/{target}/invoke", json={"inputs": {"argv": ["--version"]}}).json()
check("disable-blocks", off.get("enabled") is False and "未启用" in str(blocked), blocked)
client.patch(f"/api/v1/tools/{target}/state", json={"enabled": True, "starred": False})
hub.registry.refresh()

schema = client.get("/api/v1/tools.json").json()
check("function-schema", schema["count"] > 100 and schema["tools"][0]["function"]["name"], {"count": schema["count"]})

info = client.get("/api/v1/agent/info").json()
check("agent-info", info["counts"]["catalogue"] > 100 and "invoke" in json.dumps(info["endpoints"]), info["counts"])

guide = client.get("/api/v1/agent/guide").text
check("guide", "dry_run" in guide and len(guide) > 1200, len(guide))

# ── 旧契约四端点（其余 10 个项目那套信封），走的是同一个服务层 ──────────────────
lh = client.get("/api/health").json()
check("legacy-health", lh.get("ok") and lh.get("status") == "ok" and lh["data"]["tools"] > 100
      and set(lh["data"]["gates"]) == {"approval_mode", "high_risk_enabled"},
      {"tools": lh["data"]["tools"], "gates": lh["data"]["gates"]})

lts = client.get("/api/agent/tools", params={"limit": 50}).json()
vocab_ok = all(i["risk"] in {"read", "write", "destructive"} for i in lts["data"])
check("legacy-tools-page", lts["ok"] and lts["returned"] == 50 and lts["count_total"] > 50 and vocab_ok
      and lts["data"][0]["input_schema"]["additionalProperties"] is False,
      {"count_total": lts["count_total"], "returned": lts["returned"]})

lman = client.get("/api/agent/manifest").json()
check("legacy-manifest", lman["data"]["project"] == "WorkflowHub"
      and "/api/agent/tool" in lman["data"]["endpoints"]["tool"]
      and lman["data"]["counts"]["catalogue"] > 100, lman["data"]["counts"])

fname = schema["tools"][0]["function"]["name"]
linv = client.post("/api/agent/tool", json={"tool": fname, "input": {"dry_run": True}, "actor": "verify"}).json()
check("legacy-invoke-by-function-name", linv.get("ok") is True
      and linv["data"]["outputs"].get("command_line"), {"tool": fname, "key": linv.get("catalog_key")})

lbad = client.post("/api/agent/tool", json={"tool": "no-such-tool-xyz"})
lunde = client.post("/api/agent/tool", json={"tool": fname, "input": {"__bogus__": 1}})
check("legacy-errors-envelope", lbad.status_code == 400 and lbad.json()["error"]["code"] == "unknown_tool"
      and lunde.status_code == 400 and lunde.json()["error"]["code"] == "bad_input"
      and lunde.json()["workflowhub_code"] == "input_error",
      {"unknown": lbad.json()["error"], "undeclared": lunde.json()["error"]["code"]})

hub.catalog.update_settings({"approval_mode": True})
high_key = next((k for k, t in hub.catalog.tools.items()
                 if t.get("risk") == "high" and hub.catalog.is_enabled(k)), None)
if high_key:
    held = client.post("/api/agent/tool", json={"tool": high_key, "input": {"dry_run": True}})
    token = (held.json().get("data") or {}).get("approval", {}).get("id")
    closed = client.post(f"/api/v1/approvals/{token}/decide", json={"approve": False}).json() if token else {}
    check("legacy-approval-409", held.status_code == 409 and held.json()["error"]["code"] == "awaiting_approval"
          and closed.get("status") == "rejected", {"key": high_key, "approval": token})
hub.catalog.update_settings({"approval_mode": False})

custom = client.post("/api/v1/tools", json={"name": "smoke echo", "command": [sys.executable, "-c", "print('custom ok')"], "risk": "low"}).json()
cres = client.post(f"/api/v1/tools/{custom['id']}/invoke", json={"inputs": {}}).json()
check("custom-tool", "custom ok" in cres.get("outputs", {}).get("stdout", ""), cres.get("outputs", {}).get("stdout"))

hub.catalog.update_settings({"approval_mode": True})
shell_key = "cli-cmd" if "cli-cmd" in hub.catalog.tools else None
if shell_key:
    pend = client.post(f"/api/v1/tools/{shell_key}/invoke", json={"inputs": {"argv": ["/c", "echo hi"], "approved": False}}).json()
    check("approval-gate", pend.get("status") == "awaiting_approval", {"status": pend.get("status"), "id": (pend.get("approval") or {}).get("id")})
    tok = (pend.get("approval") or {}).get("id")
    if tok:
        dec = client.post(f"/api/v1/approvals/{tok}/decide", json={"approve": True}).json()
        check("approval-decide", dec.get("status") == "approved", dec)
hub.catalog.update_settings({"approval_mode": False})

demo = ROOT / "examples" / "text_to_file.json"
workflow = json.loads(demo.read_text(encoding="utf-8"))
r = client.post("/api/v1/workflows", json=workflow).json()
run = client.post("/api/v1/workflows/text-to-file/run", json={"version": 1, "inputs": {"text": "hello from smoke"}}).json()
check("workflow-run", run.get("status") == "succeeded" and len(run.get("node_runs", {})) == 3,
      {"status": run.get("status"), "nodes": {k: v["status"] for k, v in run.get("node_runs", {}).items()}})
check("artifact-written", Path(run["outputs"].get("result_file", "")).is_file() if run.get("outputs", {}).get("result_file") else any(
    Path(a["uri"]).is_file() for nr in run["node_runs"].values() for a in (nr.get("result") or {}).get("artifacts", [])), run.get("outputs"))

runs = client.get("/api/v1/runs", params={"limit": 5}).json()
check("runs-list", runs["items"], [i["workflow_id"] for i in runs["items"]][:3])

chain = {
    "id": "smoke-chain", "name": "冒烟：命令输出接入手写 Skill", "version": 1,
    "inputs": {"text": {"type": "string", "required": True, "description": "写入文件的内容"}},
    "outputs": {"file_path": {"type": "node_output", "node_id": "write", "field": "file_path"}},
    "nodes": [
        {"id": "probe", "type": "skill", "skill_id": hub.catalog.to_skill(target).id, "skill_version": "1.0.0",
         "inputs": {"argv": {"type": "literal", "value": ["-c", "print('chain-probe')"]},
                    "timeout_seconds": {"type": "literal", "value": 30}}},
        {"id": "write", "type": "skill", "skill_id": "file-writer", "skill_version": "1.0.0",
         "inputs": {"text": {"type": "workflow_input", "name": "text"},
                    "filename": {"type": "literal", "value": "chain.txt"}}},
    ],
    "edges": [{"source": "probe", "target": "write"}],
}
saved = client.post("/api/v1/workflows", json=chain).json()
crun = client.post("/api/v1/workflows/smoke-chain/run", json={"version": 1, "inputs": {"text": "mixed chain"}}).json()
check("mixed-dag", saved.get("valid") and crun.get("status") == "succeeded",
      {"saved": saved, "status": crun.get("status"), "err": crun.get("error")})

handler = Handler(hub)
stats = asyncio.run(handler.call("catalog_stats", {}))
check("mcp-stats", stats["total"] > 100, {"total": stats["total"], "enabled": stats["enabled"]})
found = asyncio.run(handler.call("search_tools", {"query": "curl", "limit": 3}))
check("mcp-search", found["total"] > 0, [i["key"] for i in found["items"]])
mres = asyncio.run(handler.call("invoke_tool", {"key": target, "argv": ["--version"], "actor": "mcp-smoke"}))
check("mcp-invoke", mres["status"] == "succeeded", mres.get("outputs", {}).get("command_line"))
listed = handler.tools_list()
check("mcp-tools-list", listed["meta_tools"] == 8, len(listed["tools"]))

act = client.get("/api/v1/activity").json()["items"]
check("audit-log", any(a.get("type") == "invoke" for a in act), [a.get("type") for a in act][:6])

ov = client.get("/api/v1/overview").json()
check("overview", ov["catalog"]["total"] > 100 and "recent_runs" in ov, ov["catalog"])

sysinfo = client.get("/api/v1/system/info").json()
check("system-info", sysinfo["executors"] == ["command", "python"], sysinfo["executors"])

win = client.post("/api/v1/window/action", json={"action": "maximize"}).json()
check("window-non-desktop", win.get("ok") is False, win)

bad_path = client.get("/api/v1/files", params={"path": str(Path.home() / "secret.txt")})
check("file-sandbox", bad_path.status_code == 422, bad_path.json())

# 收尾：真机验证不能在这台机器的目录里越跑越多（每次都会建一个 "smoke echo" 自定义工具）
leftovers = [k for k, t in hub.catalog.tools.items() if (t.get("title") or "") == "smoke echo"]
for key in leftovers:
    client.delete(f"/api/v1/tools/{key}")
still = [k for k, t in hub.catalog.tools.items() if (t.get("title") or "") == "smoke echo"]
mine = [a["id"] for a in client.get("/api/v1/approvals").json()["pending"]
        if a.get("requested_by") in {"agent-api", "verify"}]
for token in mine:
    client.post(f"/api/v1/approvals/{token}/decide", json={"approve": False, "actor": "verify-cleanup"})
check("cleanup-smoke-tool", len(leftovers) >= 1 and not still, {"removed": leftovers, "left": still,
                                                                "approvals_rejected": mine})

fails = [o for o in out if not o["ok"]]
print(json.dumps({"steps": len(out), "failed": len(fails), "failures": [f["step"] for f in fails]}, ensure_ascii=False))
sys.exit(1 if fails else 0)
