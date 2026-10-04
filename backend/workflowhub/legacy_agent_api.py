"""The four-endpoint Agent contract the other ZTT projects speak.

Those projects expose ``GET /api/health`` · ``GET /api/agent/tools`` · ``GET /api/agent/manifest`` ·
``POST /api/agent/tool`` with an ``{ok, data}`` / ``{ok:false, error:{code, message}}`` envelope.
Serving the same four here means an Agent config already written for that contract can drive this
machine-wide catalogue without learning ``/api/v1`` first.

Nothing is bypassed: every call lands on the same ``services.invoke_tool`` that REST, MCP and the CLI
use, so risk gating, approval mode and ``audit.jsonl`` behave identically. Two documented divergences
are intentional for a machine-wide catalogue — the tool list is paged, and a command that ran but
exited non-zero returns ``200 {ok:false}`` rather than ``5xx``, so a client cannot read it as a
transport failure and re-issue a command that already had side effects.
"""

from __future__ import annotations

import json
import time
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from .bootstrap import Hub
from .errors import WorkflowHubError
from .models import ErrorCode
from . import services

PROJECT = {"name": "WorkflowHub", "summary": "本机工具与 Workflow 编排中心，面向 AI Agent 直接调用"}

#: catalogue risk grading -> the read/write/destructive vocabulary the shared contract uses
RISK_VOCAB = {"low": "read", "medium": "write", "high": "destructive"}

DEFAULT_PAGE = 200
MAX_PAGE = 1000


def descriptor(hub: Hub, key: str) -> dict[str, Any] | None:
    """One tool in shared-contract shape, or None when the entry has no executable command."""
    try:
        skill = hub.catalog.to_skill(key)
    except (WorkflowHubError, KeyError):
        return None
    properties: dict[str, Any] = {}
    for name, spec in skill.inputs.items():
        entry: dict[str, Any] = {"type": spec.type.value, "description": spec.description or ""}
        if spec.type.value == "array":
            entry["items"] = {"type": "string"}
        properties[name] = entry
    record = hub.catalog.record(key) or {}
    return {
        "name": key,
        "function_name": skill.id.replace("-", "_"),
        "description": f"{skill.name} — {skill.description}"[:900],
        "input_schema": {"type": "object", "properties": properties,
                         "required": [n for n, s in skill.inputs.items() if s.required],
                         "additionalProperties": False},
        "risk": RISK_VOCAB.get(str(record.get("risk") or skill.risk_level.value), "read"),
        "title": record.get("title"),
        "kind": record.get("kind"),
        "enabled": hub.catalog.is_enabled(key),
    }


def create_router(hub: Hub, started_at: float | None = None) -> APIRouter:
    router = APIRouter()
    booted = started_at or time.time()
    alias: dict[str, str] = {}
    alias_size = -1

    def resolve(name: str) -> str | None:
        """Accept the catalogue key, the ``-``/``_`` spellings and the function-calling name."""
        nonlocal alias_size
        if not name:
            return None
        if name in hub.catalog.tools:
            return name
        hyphen = name.replace("_", "-")
        if hyphen in hub.catalog.tools:
            return hyphen
        if alias_size != len(hub.catalog.tools):
            alias.clear()
            for key in hub.catalog.tools:
                alias[key] = key
                alias[key.replace("-", "_")] = key
                try:
                    skill_id = hub.catalog.to_skill(key).id
                except (WorkflowHubError, KeyError):
                    continue
                alias.setdefault(skill_id, key)
                alias.setdefault(skill_id.replace("-", "_"), key)
            alias_size = len(hub.catalog.tools)
        return alias.get(name) or alias.get(hyphen)

    def json_env(status_code: int, code: str, message: str, **extra: Any) -> JSONResponse:
        return JSONResponse({"ok": False, "error": {"code": code, "message": message}, **extra},
                            status_code=status_code)

    def from_hub_error(exc: WorkflowHubError) -> tuple[int, str, str, dict[str, Any]]:
        detail = exc.detail
        if detail.code == ErrorCode.PERMISSION_ERROR:
            status, code = 403, "permission_denied"
        elif detail.code in (ErrorCode.INPUT_ERROR, ErrorCode.VALIDATION_ERROR):
            status, code = 400, "bad_input"
        elif detail.code == ErrorCode.TIMEOUT:
            status, code = 504, "timeout"
        else:
            status, code = 500, "handler_failed"
        return status, code, detail.message, {
            "workflowhub_code": detail.code.value,
            "retryable": detail.retryable,
            "details": detail.details,
        }

    @router.get("/api/health")
    def health(request: Request) -> dict[str, Any]:
        facets = hub.catalog.facets()
        data = {
            "project": PROJECT["name"],
            "version": hub.version(),
            "agent_api": 1,
            "tools": facets["total"],
            "enabled": facets["enabled"],
            "uptime_ms": int((time.time() - booted) * 1000),
            "base_url": str(request.base_url).rstrip("/"),
            "gates": {"approval_mode": hub.catalog.settings["approval_mode"],
                      "high_risk_enabled": hub.catalog.settings["high_risk_enabled"]},
        }
        # 老客户端拿顶层 status=="ok" 当存活探针，这里同时给出旧键与标准信封
        return {"ok": True, "status": "ok", "version": hub.version(), "data": data}

    @router.get("/api/agent/tools")
    def agent_tools(query: str = "", limit: int = DEFAULT_PAGE, offset: int = 0,
                    enabled_only: bool = Query(True, alias="enabled")) -> dict[str, Any]:
        needle = query.lower()
        keys = [key for key, tool in hub.catalog.tools.items()
                if (not enabled_only or hub.catalog.is_enabled(key))
                and (not needle or needle in
                     f"{key} {tool.get('title') or ''} {tool.get('description') or ''}".lower())]
        limit = max(1, min(limit, MAX_PAGE))
        offset = max(0, offset)
        items = [row for key in keys[offset:offset + limit] if (row := descriptor(hub, key))]
        return {
            "ok": True,
            "data": items,
            "count_total": len(keys),
            "returned": len(items),
            "offset": offset,
            "limit": limit,
            "hint": f"目录共 {len(keys)} 条，本面只是其中一段；翻页用 offset，"
                    "全量 schema 用 GET /api/v1/tools.json，搜索用 GET /api/v1/tools?query=",
        }

    @router.get("/api/agent/manifest")
    def agent_manifest(request: Request) -> dict[str, Any]:
        facets = hub.catalog.facets()
        tools = [row for key in list(hub.catalog.tools)[:DEFAULT_PAGE]
                 if (row := descriptor(hub, key))]
        return {
            "ok": True,
            "data": {
                "project": PROJECT["name"],
                "version": hub.version(),
                "description": PROJECT["summary"],
                "base_url": str(request.base_url).rstrip("/"),
                "agent_api": 1,
                "counts": {"catalogue": facets["total"], "enabled": facets["enabled"],
                           "handwritten_skills": len(hub.directory.list())},
                "tools": tools,
                "endpoints": {
                    "health": "/api/health",
                    "tools": "/api/agent/tools",
                    "manifest": "/api/agent/manifest",
                    "tool": "POST /api/agent/tool",
                    "native": "/api/v1/*（REST）· /api/v1/tools.json（function schema）· MCP stdio · CLI",
                },
                "conventions": {
                    "invoke_body": {"tool": "<name>", "input": {"argv": ["--version"], "dry_run": False},
                                    "actor": "调用方名字，会写进审计"},
                    "paging": "tools 是分页视图，不是全量",
                    "failed_command": "命令跑通但退出码非 0 → HTTP 200 + ok:false"
                                      "（不当成传输错误，避免客户端重发已有副作用的命令）",
                    "risk": RISK_VOCAB,
                },
                "gates": {"approval_mode": hub.catalog.settings["approval_mode"],
                          "high_risk_enabled": hub.catalog.settings["high_risk_enabled"]},
            },
        }

    @router.post("/api/agent/tool")
    async def agent_tool(request: Request) -> Any:
        raw = await request.body()
        if len(raw) > 8 * 1024 * 1024:
            return json_env(400, "too_large", "请求体超过 8MB")
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            return json_env(400, "bad_json", "请求体不是合法 JSON")
        if not isinstance(body, dict):
            return json_env(400, "bad_input", "请求体必须是 JSON 对象")

        name = str(body.get("tool") or body.get("name") or "")
        key = resolve(name)
        if key is None:
            return json_env(400, "unknown_tool", f"未注册的工具：{name or '(空)'}",
                            available=list(hub.catalog.tools)[:40],
                            hint="GET /api/agent/tools 取名单，GET /api/v1/tools?query= 搜索")

        payload = body.get("input")
        if payload is None:
            payload = body.get("inputs") or body.get("args") or {}
        if not isinstance(payload, dict):
            return json_env(400, "bad_input", "input 必须是对象")
        inputs = dict(payload)
        if body.get("dry_run") and "dry_run" not in inputs:
            inputs["dry_run"] = True

        started = time.time()
        try:
            result = await services.invoke_tool(hub, key, inputs, str(body.get("actor") or "agent-api"))
        except WorkflowHubError as exc:
            status, code, message, extra = from_hub_error(exc)
            return json_env(status, code, message, **extra)

        ms = int((time.time() - started) * 1000)
        data = {"status": result.get("status"), "run_id": result.get("run_id"),
                "trace_id": result.get("trace_id"), "tool": result.get("tool"),
                "approval": result.get("approval"),
                "outputs": result.get("outputs"), "artifacts": result.get("artifacts"),
                "logs": result.get("logs")}
        status = result.get("status")
        if status == "awaiting_approval":
            return json_env(409, "awaiting_approval",
                            "审批模式已开启：请在 WorkflowHub 窗口批准，再带 approved=true 重试",
                            data=data)
        if status and status != "succeeded":
            error = result.get("error") or {}
            return json_env(200, "tool_failed", error.get("message") or f"工具执行状态：{status}",
                            data=data, ms=ms)
        return {"ok": True, "tool": name, "catalog_key": key, "ms": ms, "data": data}

    return router
