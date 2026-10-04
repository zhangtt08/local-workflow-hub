"""HTTP boundary for the desktop app, the UI and external Agents.

Everything an Agent needs is under ``/api/v1``; the same service functions back the MCP server
and the CLI, so the three entry points cannot drift apart.
"""

from __future__ import annotations

import json
import os
import platform
import socket
import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .agent_docs import GUIDE, QUICKSTART
from .bootstrap import Hub, build_hub, resource_dir, static_root
from .catalog import DEFAULT_SETTINGS
from .errors import WorkflowHubError
from .legacy_agent_api import create_router as legacy_router
from .models import ErrorCode, WorkflowDefinition
from . import services


class InvokeRequest(BaseModel):
    inputs: dict[str, Any] = Field(default_factory=dict)
    actor: str = "agent"
    wait: bool = True


class ScanRequest(BaseModel):
    roots: list[str] | None = None
    wait: bool = False


class ToolStateRequest(BaseModel):
    enabled: bool | None = None
    starred: bool | None = None
    exclusive: bool | None = None


class NoteRequest(BaseModel):
    notes: str = ""
    alias: str | None = None


class CustomToolRequest(BaseModel):
    name: str
    command: list[str]
    description: str = ""
    cwd: str | None = None
    category: str = "自定义工具"
    tags: list[str] = Field(default_factory=list)
    risk: str = "medium"


class SettingsRequest(BaseModel):
    patch: dict[str, Any] = Field(default_factory=dict)


class ApprovalDecision(BaseModel):
    approve: bool
    actor: str = "user"


def error_payload(exc: WorkflowHubError) -> dict[str, Any]:
    return {"error": exc.detail.model_dump(mode="json")}


def create_app(hub: Hub | None = None) -> FastAPI:
    hub = hub or build_hub()
    if not services.list_workflows(hub):
        # 首次运行给一个可跑的示例，界面不会是空白页
        example = resource_dir("examples") / "text_to_file.json"
        if example.is_file():
            try:
                hub.workflows.save_version(WorkflowDefinition.model_validate_json(example.read_text(encoding="utf-8")))
            except ValueError:
                pass
    app = FastAPI(title="WorkflowHub", version=hub.version(),
                  description="本机工具与Workflow编排中心，面向 AI Agent 直接调用")
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
    app.state.hub = hub
    app.state.window = None
    token = os.environ.get("WORKFLOWHUB_TOKEN", "").strip()

    @app.middleware("http")
    async def bearer(request: Request, call_next):
        if token and request.url.path.startswith("/api/") and request.url.path != "/health":
            provided = request.headers.get("x-workflowhub-token", "")
            if provided != token:
                return JSONResponse({"error": {"code": "permission_error",
                                               "message": "缺少 X-WorkflowHub-Token", "details": {}}},
                                    status_code=401)
        return await call_next(request)

    @app.exception_handler(WorkflowHubError)
    async def hub_error(_: Request, exc: WorkflowHubError):
        return JSONResponse(error_payload(exc), status_code=422)

    @app.exception_handler(Exception)
    async def unhandled(_: Request, exc: Exception):
        code = 501 if isinstance(exc, NotImplementedError) else 500
        return JSONResponse({"error": {"code": "unknown_error", "message": f"{type(exc).__name__}: {exc}",
                                       "retryable": False, "details": {}}}, status_code=code)

    @app.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException):
        detail = exc.detail
        if isinstance(detail, dict):
            return JSONResponse({"error": detail}, status_code=exc.status_code)
        return JSONResponse({"error": {"code": "validation_error", "message": str(detail),
                                      "retryable": False, "details": {}}}, status_code=exc.status_code)

    def not_found(what: str, key: str) -> HTTPException:
        # Agent 常把 function-calling 的下划线名直接当 key 用；给出确切拼写，让它自己能纠正
        guess = key.replace("_", "-") if "_" in key else key.replace("-", "_")
        hint = f"是不是 {guess}？" if guess in hub.catalog.tools else "用 GET /api/v1/tools?query= 搜索确切 key"
        return HTTPException(status_code=404, detail={"code": "validation_error",
                                                      "message": f"未找到{what}：{key}（{hint}）", "details": {}})

    # 与其余 10 个项目同形的四端点旧契约（同一服务层，风险闸门与审计照旧）
    app.include_router(legacy_router(hub))

    # ── meta ───────────────────────────────────────────────────────────────
    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "version": hub.version(), "instance": hub.instance,
                "tools": hub.catalog.facets()["total"],
                "enabled": hub.catalog.facets()["enabled"]}

    @app.get("/api/v1/overview")
    def overview() -> dict[str, Any]:
        return services.overview(hub)

    @app.get("/api/v1/agent/info")
    def agent_info(request: Request) -> dict[str, Any]:
        base = str(request.base_url).rstrip("/")
        return {
            "name": "WorkflowHub", "version": hub.version(), "base_url": base,
            "protocol": ["rest-json", "mcp-stdio", "function-calling-schema"],
            "auth": {"required": bool(token), "header": "X-WorkflowHub-Token" if token else None},
            "counts": {"catalogue": hub.catalog.facets()["total"], "enabled": hub.catalog.facets()["enabled"],
                       "handwritten_skills": len(hub.directory.list()), "workflows": len(services.list_workflows(hub))},
            "endpoints": {
                "tools": "/api/v1/tools", "tool_detail": "/api/v1/tools/{key}",
                "invoke": "/api/v1/tools/{key}/invoke", "schema": "/api/v1/tools.json",
                "workflows": "/api/v1/workflows", "run_workflow": "/api/v1/workflows/{id}/run",
                "runs": "/api/v1/runs", "guide": "/api/v1/agent/guide", "mcp_config": "/api/v1/agent/mcp-config",
                "legacy_agent_contract": "/api/health · /api/agent/tools · /api/agent/manifest · POST /api/agent/tool",
                "openapi": "/openapi.json",
            },
            "conventions": {
                "invoke_body": {"inputs": {"argv": ["--flag", "value"], "stdin": "…", "dry_run": False},
                                "actor": "调用方名字，用于审计", "wait": "false 时返回 task_id 异步轮询"},
                "generic_inputs": sorted(services.GENERIC_INPUTS - {"approved"}),
                "errors": "非 2xx 返回 {\"error\": {code, message, retryable, details}}",
            },
            "risk": {"approval_mode": hub.catalog.settings["approval_mode"],
                     "high_risk_enabled": hub.catalog.settings["high_risk_enabled"]},
        }

    @app.get("/api/v1/agent/guide", response_class=PlainTextResponse)
    def agent_guide() -> str:
        return GUIDE

    @app.get("/api/v1/agent/quickstart", response_class=PlainTextResponse)
    def quickstart() -> str:
        return QUICKSTART

    @app.get("/api/v1/agent/mcp-config")
    def mcp_config() -> dict[str, Any]:
        frozen = bool(getattr(sys, "frozen", False))
        command = str(Path(sys.executable).parent / "WorkflowHub.exe") if frozen else "python"
        args = ["--mcp"] if frozen else ["-m", "workflowhub", "mcp"]
        return {"mcpServers": {"workflowhub": {"command": command, "args": args,
                                               "env": {"WORKFLOWHUB_DATA": str(hub.roots["data"])}}},
                "note": "STDIO 传输：Agent 直接以子进程方式连接，工具清单实时来自本机目录"}

    @app.get("/api/v1/tools.json")
    def tools_schema(enabled_only: bool = Query(True)) -> dict[str, Any]:
        tools = hub.catalog.agent_tool_schema(enabled_only)
        return {"tools": tools, "count": len(tools),
                "usage": "把 tools 数组直接交给 OpenAI/DeepSeek/Anthropic 的 function calling；"
                         "调用时用 function.name 反查 /api/v1/tools/{key}"}

    @app.get("/api/v1/agent/snippets/{key}")
    def snippet_bundle(key: str, request: Request, example: str = "{}") -> dict[str, Any]:
        if key not in hub.catalog.tools:
            raise not_found("工具", key)
        try:
            inputs = json.loads(example)
        except json.JSONDecodeError as exc:
            raise WorkflowHubError(ErrorCode.INPUT_ERROR, f"example 不是合法 JSON：{exc}") from exc
        return services.snippets(str(request.base_url).rstrip("/"), key,
                                 services.resolve_skill_id(hub, key), inputs)

    # ── catalogue ──────────────────────────────────────────────────────────
    @app.get("/api/v1/tools")
    def list_tools(query: str = "", category: str | None = None, kind: str | None = None,
                   risk: str | None = None, enabled: bool | None = None, starred: bool | None = None,
                   limit: int = 60, offset: int = 0) -> dict[str, Any]:
        return hub.catalog.list(query, category, kind, risk, enabled, starred, limit, offset)

    @app.get("/api/v1/tools/{key}")
    def tool_detail(key: str, request: Request) -> dict[str, Any]:
        if key not in hub.catalog.tools:
            raise not_found("工具", key)
        record = hub.catalog.record(key) or {}
        try:
            skill = hub.catalog.to_skill(key)
            contract = skill.model_dump(mode="json")
        except WorkflowHubError as exc:
            skill, contract = None, None
            record["blueprint_hint"] = exc.detail.message
        return {
            "tool": record,
            "skill": contract,
            "invocation": {"endpoint": f"/api/v1/tools/{key}/invoke",
                           "fields": {name: spec.model_dump(mode="json") for name, spec in skill.inputs.items()} if skill else {},
                           "generic": sorted(services.GENERIC_INPUTS - {"approved"}),
                           "example": {"inputs": {"argv": [], "dry_run": True}}},
            "snippets": services.snippets(str(request.base_url).rstrip("/"), key,
                                          skill.id if skill else key, {"argv": [], "dry_run": True}),
            "audit": [row for row in hub.catalog.recent_activity(200) if row.get("tool") == key][:12],
        }

    @app.post("/api/v1/tools/{key}/invoke")
    async def invoke(key: str, body: InvokeRequest) -> dict[str, Any]:
        if key not in hub.catalog.tools:
            raise not_found("工具", key)
        if not body.wait:
            return services.start_background_invoke(hub, key, body.inputs, body.actor, "rest")
        return await services.invoke_tool(hub, key, body.inputs, body.actor, "rest")

    @app.post("/api/v1/tools/{key}/adopt")
    def adopt_skill_package(key: str, actor: str = "ui") -> dict[str, Any]:
        try:
            result = hub.catalog.adopt_skill_package(key, hub.roots["skills"])
        except KeyError as exc:
            raise not_found("工具", key) from exc
        except WorkflowHubError:
            raise
        except Exception as exc:
            raise WorkflowHubError(ErrorCode.EXECUTOR_ERROR, f"接入失败：{exc}") from exc
        hub.registry.refresh()
        hub.catalog.audit({"type": "tool_adopt", "tool": key, "actor": actor, "path": result["path"]})
        return {**result, "skills_now": len(hub.directory.list()),
                "hint": "已复制到 skills/，现在可以像手写 Skill 一样编排进 Workflow"}

    @app.post("/api/v1/tools/{key}/probe")
    def probe(key: str, actor: str = "ui") -> dict[str, Any]:
        """探测版本会**真的跑一次**这条命令（带 --version），所以同样要进审计。"""
        try:
            result = hub.catalog.probe(key)
        except KeyError as exc:
            raise not_found("工具", key) from exc
        hub.catalog.audit({"type": "probe", "tool": key, "actor": actor,
                           "command_line": result.get("command_line"), "ok": result.get("ok")})
        return result

    @app.patch("/api/v1/tools/{key}/state")
    def tool_state(key: str, body: ToolStateRequest, actor: str = "ui") -> dict[str, Any]:
        try:
            row = dict(hub.catalog.record(key) or {})
            for flag, value in (("enabled", body.enabled), ("starred", body.starred),
                                ("exclusive", body.exclusive)):
                if value is not None:
                    before = row.get(flag)
                    row = hub.catalog.set_flag(key, flag, value)
                    if before != value:
                        hub.catalog.audit({"type": "tool_state", "tool": key, "actor": actor,
                                           "flag": flag, "from": before, "to": value})
            hub.registry.refresh()
            return row
        except KeyError as exc:
            raise not_found("工具", key) from exc

    @app.put("/api/v1/tools/{key}/note")
    def tool_note(key: str, body: NoteRequest) -> dict[str, Any]:
        try:
            return hub.catalog.set_note(key, body.notes, body.alias)
        except KeyError as exc:
            raise not_found("工具", key) from exc
        except ValueError as exc:
            raise WorkflowHubError(ErrorCode.INPUT_ERROR, str(exc)) from exc

    @app.post("/api/v1/tools")
    def add_custom(body: CustomToolRequest, actor: str = "ui") -> dict[str, Any]:
        try:
            row = hub.catalog.add_custom(body.model_dump())
        except ValueError as exc:
            raise WorkflowHubError(ErrorCode.INPUT_ERROR, str(exc)) from exc
        hub.registry.refresh()
        hub.catalog.audit({"type": "tool_add", "tool": row.get("key") or row.get("id"), "actor": actor,
                           "command_line": " ".join(body.command), "risk": row.get("risk")})
        return row

    @app.delete("/api/v1/tools/{key}")
    def delete_tool(key: str, actor: str = "ui") -> dict[str, str]:
        try:
            gone = dict(hub.catalog.record(key) or {})
            hub.catalog.remove(key)
        except KeyError as exc:
            raise not_found("工具", key) from exc
        except ValueError as exc:
            raise WorkflowHubError(ErrorCode.PERMISSION_ERROR, str(exc),
                                   {"hint": "扫描到的工具不能删除，可用 PATCH state 关闭"}) from exc
        hub.registry.refresh()
        hub.catalog.audit({"type": "tool_remove", "tool": key, "actor": actor,
                           "command_line": " ".join(gone.get("command") or [])})
        return {"status": "deleted", "key": key}

    # ── discovery + settings ───────────────────────────────────────────────
    @app.post("/api/v1/discovery/scan")
    def scan(body: ScanRequest, actor: str = "ui") -> dict[str, Any]:
        result = hub.catalog.scan(body.roots, wait=body.wait)
        hub.registry.refresh()
        hub.catalog.audit({"type": "scan", "actor": actor, "roots": body.roots or hub.catalog.settings["scan_roots"],
                           "result": result})
        return {"result": result, "stats": services.catalogue_stats(hub)}

    @app.get("/api/v1/discovery/status")
    def scan_status() -> dict[str, Any]:
        return {"scan_state": hub.catalog.scan_state, "last_scan": hub.catalog.last_scan,
                "errors": hub.catalog.scan_errors, "roots": hub.catalog.settings["scan_roots"],
                "stats": services.catalogue_stats(hub)}

    @app.get("/api/v1/settings")
    def get_settings() -> dict[str, Any]:
        return {"settings": hub.catalog.settings, "defaults": DEFAULT_SETTINGS}

    @app.put("/api/v1/settings")
    def put_settings(body: SettingsRequest, actor: str = "ui") -> dict[str, Any]:
        before = dict(hub.catalog.settings)
        after = hub.catalog.update_settings(body.patch)
        changed = {k: {"from": before.get(k), "to": after.get(k)} for k, v in body.patch.items()
                   if after.get(k) != before.get(k)}
        if changed:
            # 改开关是要留痕的动作：谁把高风险放开、谁把审批关掉，事后要查得回
            hub.catalog.audit({"type": "settings", "actor": actor, "changed": changed})
        return {"settings": after, "changed": sorted(changed)}

    @app.get("/api/v1/activity")
    def activity(limit: int = 40, tool: str | None = None, actor: str | None = None,
                 type: str | None = Query(None, alias="type")) -> dict[str, Any]:
        """审计流水。查"这把工具最近被谁调过"要能按 tool/actor/type 过滤，
        而不是让人在 40 条混合流水里肉眼找。"""
        return {"items": hub.catalog.recent_activity(limit, tool=tool, actor=actor, event_type=type),
                "filters": {"tool": tool, "actor": actor, "type": type},
                "audit": hub.catalog.audit_stats()}

    @app.get("/api/v1/tasks")
    def tasks(running_only: bool = True) -> dict[str, Any]:
        """当前/最近的任务与在跑的调用：长任务要看得见"还在跑、跑了多久、哪一步"。"""
        rows = []
        for task_id in list(services.TASKS):
            state = services.task_state(task_id)
            if state is None:
                continue
            if running_only and state.get("status") != "running":
                continue
            rows.append(state)
        return {"items": sorted(rows, key=lambda r: r.get("started_at") or ""),
                "live_calls": services.running_calls(),
                "note": "elapsed_seconds 是本次调用已用时间；取消用 POST /api/v1/tasks/{id}/cancel"}

    @app.get("/api/v1/tasks/{task_id}")
    def task(task_id: str) -> dict[str, Any]:
        state = services.task_state(task_id)
        if state is None:
            raise not_found("任务", task_id)
        return state

    @app.post("/api/v1/tasks/{task_id}/cancel")
    def cancel_task(task_id: str, actor: str = "ui") -> dict[str, Any]:
        """真的停：掐掉协程 → 执行器按进程树杀掉命令及它自己派生的子进程。

        只把状态改成 cancelled 不算停 —— 那样进程还在机器上跑，用户看到的"已取消"是假的。
        """
        result = services.cancel_task(task_id, actor)
        if result is None:
            raise not_found("任务", task_id)
        hub.catalog.audit({"type": "task_cancel", "tool": result.get("tool"), "actor": actor,
                           "task_id": task_id, "reason": result.get("reason")})
        return result

    # ── approvals ──────────────────────────────────────────────────────────
    @app.get("/api/v1/approvals")
    def approvals() -> dict[str, Any]:
        return {"pending": hub.catalog.pending_approvals()}

    @app.post("/api/v1/approvals/{token_id}/decide")
    def decide(token_id: str, body: ApprovalDecision) -> dict[str, Any]:
        try:
            return hub.catalog.decide_approval(token_id, body.approve, body.actor)
        except KeyError as exc:
            raise not_found("审批请求", token_id) from exc

    # ── skills + workflows + runs ──────────────────────────────────────────
    @app.get("/api/v1/skills")
    def skills(category: str | None = None, query: str = "") -> dict[str, Any]:
        rows = [s.model_dump(mode="json") for s in hub.directory.list()]
        if category:
            rows = [r for r in rows if r["category"] == category]
        if query:
            hits = {s.id for s in hub.registry.search(query)}
            rows = [r for r in rows if r["id"] in hits]
        return {"items": rows, "total": len(rows)}

    @app.get("/api/v1/skills/{skill_id}")
    def skill_detail(skill_id: str, version: str | None = None) -> dict[str, Any]:
        skill = hub.registry.get_version(skill_id, version) if version else hub.registry.get(skill_id)
        if skill is None:
            raise not_found("Skill", skill_id)
        return skill.model_dump(mode="json")

    @app.post("/api/v1/workflows")
    def save_workflow(workflow: WorkflowDefinition) -> dict[str, Any]:
        report = hub.runner.validator.validate(workflow)
        if not report.valid:
            raise WorkflowHubError(ErrorCode.VALIDATION_ERROR, "；".join(report.errors),
                                   {"errors": report.errors, "warnings": report.warnings})
        try:
            hub.workflows.save_version(workflow)
        except ValueError as exc:
            raise WorkflowHubError(ErrorCode.VALIDATION_ERROR, str(exc)) from exc
        return {"workflow_version_id": workflow.version_id, "warnings": report.warnings,
                "errors": report.errors, "valid": report.valid}

    @app.post("/api/v1/workflows/validate")
    def validate_workflow(workflow: WorkflowDefinition) -> dict[str, Any]:
        report = hub.runner.validator.validate(workflow)
        return {"valid": report.valid, "errors": report.errors, "warnings": report.warnings,
                "nodes": [{"id": n.id, "skill": n.skill_id, "version": n.skill_version} for n in workflow.nodes]}

    @app.get("/api/v1/workflows")
    def workflows() -> dict[str, Any]:
        return {"items": services.list_workflows(hub)}

    @app.get("/api/v1/workflows/{workflow_id}")
    def get_workflow(workflow_id: str, version: int = 1) -> dict[str, Any]:
        workflow = hub.workflows.get_version(workflow_id, version)
        if workflow is None:
            raise not_found("Workflow", f"{workflow_id}@{version}")
        return workflow.model_dump(mode="json")

    @app.post("/api/v1/workflows/{workflow_id}/run")
    async def run_workflow(workflow_id: str, body: dict[str, Any]) -> dict[str, Any]:
        version = int(body.get("version", 1))
        workflow = hub.workflows.get_version(workflow_id, version)
        if workflow is None:
            raise not_found("Workflow", f"{workflow_id}@{version}")
        run = await hub.runner.run(workflow, body.get("inputs") or {})
        hub.catalog.audit({"type": "workflow_run", "tool": workflow_id, "actor": body.get("actor", "agent"),
                           "run_id": run.id, "status": run.status.value})
        return run.model_dump(mode="json")

    @app.get("/api/v1/runs")
    def runs(limit: int = 25, status: str | None = None) -> dict[str, Any]:
        return {"items": services.list_runs(hub, limit, status)}

    @app.get("/api/v1/runs/{run_id}")
    def get_run(run_id: str) -> dict[str, Any]:
        try:
            run = hub.runs.get(run_id)
        except ValueError as exc:
            raise WorkflowHubError(ErrorCode.INPUT_ERROR, f"非法 run_id：{exc}") from exc
        if run is None:
            raise not_found("Run", run_id)
        return run.model_dump(mode="json")

    @app.get("/api/v1/files")
    def read_file(path: str, download: bool = False) -> Any:
        target = Path(path).resolve()
        allowed = [hub.roots["data"].resolve(), (hub.roots["project"] / "skills").resolve()]
        if not any(target.is_relative_to(root) for root in allowed):
            raise WorkflowHubError(ErrorCode.PERMISSION_ERROR, "只能读取 data/ 或 skills/ 内的文件",
                                   {"path": str(target)})
        if not target.is_file():
            raise not_found("文件", str(target))
        if download:
            return FileResponse(target, filename=target.name)
        text = target.read_text(encoding="utf-8", errors="replace")
        return {"path": str(target), "size": target.stat().st_size, "text": text[:200_000],
                "truncated": len(text) > 200_000}

    # ── system ─────────────────────────────────────────────────────────────
    @app.get("/api/v1/system/info")
    def system_info() -> dict[str, Any]:
        return {
            "app": {"version": hub.version(), "frozen": bool(getattr(__import__("sys"), "frozen", False)),
                    "desktop_mode": os.environ.get("WORKFLOWHUB_DESKTOP") == "1"},
            "machine": {"system": platform.system(), "release": platform.release(),
                        "node": platform.node(), "python": platform.python_version(),
                        "hostname_ip": socket.gethostbyname(socket.gethostname()) if False else None},
            "paths": {"project": str(hub.roots["project"]), "data": str(hub.roots["data"]),
                      "skills": str(hub.roots["skills"]), "ui": str(static_root() or "")},
            "counts": hub.catalog.facets(),
            "executors": sorted(hub.executors.types),
            "token_required": bool(token),
        }

    @app.get("/api/v1/search")
    def search(query: str, top_k: int = 24) -> dict[str, Any]:
        skills = hub.registry.search(query, top_k)
        return {"skills": [{"id": s.id, "name": s.name, "category": s.category,
                            "description": s.description, "risk": s.risk_level.value} for s in skills]}

    # ── desktop window bridge (only live when launched as the desktop app) ────
    def window() -> Any:
        return getattr(app.state, "window", None)

    @app.get("/api/v1/window/state")
    def window_state() -> dict[str, Any]:
        controller = window()
        if controller is None:
            return {"desktop_mode": False, "state": None}
        return {"desktop_mode": True, "state": controller.state()}

    @app.post("/api/v1/window/measure")
    def window_measure(body: dict[str, Any]) -> dict[str, Any]:
        controller = window()
        if controller is None:
            return {"desktop_mode": False}
        return {"desktop_mode": True, "state": controller.measure(
            float(body.get("inner_height", 0)), float(body.get("device_pixel_ratio", 1)))}

    @app.post("/api/v1/window/action")
    def window_action(body: dict[str, Any]) -> dict[str, Any]:
        controller = window()
        if controller is None:
            return {"ok": False, "desktop_mode": False,
                    "reason": "当前是浏览器预览，窗口按钮不可控制桌面窗口"}
        return controller.act(str(body.get("action", "")), body.get("payload") or {})

    @app.get("/api/v1/window/capture")
    def window_capture(path: str = "") -> dict[str, Any]:
        controller = window()
        if controller is None:
            return {"ok": False, "reason": "非桌面模式"}
        target = Path(path) if path else hub.roots["data"] / "window.png"
        return {"ok": controller.capture(target), "path": str(target)}

    @app.post("/api/v1/system/quit")
    def quit_app() -> dict[str, str]:
        """浏览器模式下界面里的「退出服务」：落一个停止标记，桌面循环退出后进程自然结束。"""
        marker = hub.roots["data"] / "desktop.stop"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("quit", encoding="utf-8")
        controller = window()
        if controller is not None:
            controller.close()
        return {"status": "stopping"}

    ui = static_root()
    if ui:
        app.mount("/", StaticFiles(directory=str(ui), html=True), name="ui")
    else:
        @app.get("/")
        def missing_ui() -> JSONResponse:
            return JSONResponse({"message": "前端尚未构建：在 frontend 目录执行 npm install && npm run build",
                                 "api": "/docs", "health": "/health"})
    return app


app = create_app()
