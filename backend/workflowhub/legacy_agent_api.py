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

#: 共享契约要求 `name` 全局唯一并带 `<project-id>.` 前缀（AGENT_API_STANDARD.md 第 1 条）。
#: 目录里的原始 key（cli-ffmpeg 之类）在本机是唯一的，但放到整个生态里就不是了 —— 所以对外加前缀，
#: 入参两个写法都认（带前缀的标准名，和目录原始 key），老客户端不会被这次改名打断。
NAME_PREFIX = "workflowhub."

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
    risk = RISK_VOCAB.get(str(record.get("risk") or skill.risk_level.value), "read")
    if risk in ("exec", "destructive"):
        # 共享契约里"这一次是有副作用的，我确认"叫 confirm；本项目的审批门叫 approved
        # （services.py 的 needs_approval 那一道，审批模式开着就不带不执行）。
        # 描述符必须把这个入参报出来，否则调用方看 schema 根本不知道自己需要给。
        properties["confirm"] = {
            "type": "boolean",
            "description": "有副作用的调用：必须显式 confirm=true（等价于 approved=true），"
                           "审批模式开着时不带就按 permission_denied 拒掉",
        }
    return {
        "name": f"{NAME_PREFIX}{key}",
        "catalogue_key": key,
        "function_name": skill.id.replace("-", "_"),
        "description": f"{skill.name} — {skill.description}"[:900],
        "input_schema": {"type": "object", "properties": properties, "required": [n for n, s in skill.inputs.items() if s.required],
                         "additionalProperties": False},
        "risk": risk,
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
        if name.startswith(NAME_PREFIX):
            name = name[len(NAME_PREFIX):]      # 对外是带前缀的标准名，目录里没有这个前缀
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
        # available 必须放在 error 里面：调用方（以及生态验收器）是在 error 上找可用名单的，
        # 放顶层等于让"这个工具不存在"的回执变成查不到出处的信息。
        available = extra.pop("available", None)
        error: dict[str, Any] = {"code": code, "message": message}
        if available is not None:
            error["available"] = available
        return JSONResponse({"ok": False, "error": error, **extra}, status_code=status_code)

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
                    "tool_name": "tools 里的 name 带 workflowhub. 前缀（共享契约要求全局唯一）；"
                                 "POST /api/agent/tool 带不带前缀都认，目录原始 key 也能直接用",
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
        # 契约的 confirm 与本项目审批门的 approved 是同一件事的两个名字：改名送进同一道判定，
        # 判定本身一点不放宽（needs_approval 开着而不带 → 照样 permission_denied）。
        # confirm 要从入参里摘掉再往下走，它不在工具的 GENERIC_INPUTS 里，留着会被当成未知入参拒了。
        if inputs.get("confirm") is True:
            inputs.setdefault("approved", True)
        if "confirm" in inputs:
            inputs.pop("confirm")
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
