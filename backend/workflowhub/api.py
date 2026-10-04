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
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .agent_docs import GUIDE, QUICKSTART
from .bootstrap import Hub, build_hub, resource_dir, static_root
from .catalog import DEFAULT_SETTINGS
from .errors import WorkflowHubError
from .legacy_agent_api import create_router as legacy_router
from .local_guard import (SESSION_COOKIE, TOKEN_HEADER, ensure_token, host_problem,
                          max_body_bytes, origin_problem, require_within, token_matches,
                          token_path)
from .models import ErrorCode, WorkflowDefinition
from .redaction import redact_wire
from . import services

#: 会改东西的方法：这些才要口令（共享标准 (d)：非 GET 一律验口令）
_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _bound_port(request: Request) -> int | None:
    """这个进程**真的**绑在哪个端口 —— 取自 ASGI scope，不读配置。

    桌面端每次双击都是随机端口，把期望端口写死在配置里就会判错；uvicorn 与
    `TestClient(base_url=…)` 都会把真实绑定地址填进 `scope["server"]`。
    只有 server host 本身是回环字面量时才拿它当基准（`TestClient` 默认 host 是
    "testserver"，那种情况下端口不可信，但 Host 那条判据已经会先拒掉它）。
    """
    server = request.scope.get("server") or ()
    if len(server) >= 2 and str(server[0]).lower() in {"127.0.0.1", "localhost", "::1"}:
        try:
            port = int(server[1])
        except (TypeError, ValueError):
            return None
        return port or None
    return None


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


def _readable_roots(hub: Hub) -> list[Path]:
    """data/ 与 skills/：读文件与写截图共用这一份名单。"""
    return [hub.roots["data"].resolve(), (hub.roots["project"] / "skills").resolve()]


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
    # 这里**不再**挂 CORSMiddleware。改之前是 allow_origins=["*"] + allow_headers=["*"]：
    # 界面与自己是同源的，根本不需要 CORS，而"任何源都可以"等于把 1,529 个本机能力
    # 交给浏览器里打开的任何一个网页（共享标准第 (e) 条：状态变更路由永不允许 `*`）。
    # 需要给别的源开读权限时用 WORKFLOWHUB_ALLOWED_HOSTS 显式加白，判据见 local_guard。
    app.state.hub = hub
    app.state.window = None
    # 口令默认**必配**：没有环境变量就在首次启动时生成一把，落盘（0600）后再也不变。
    # 之前是"配了才校验"，而默认是没配 —— 于是那条校验从来没生效过。
    token, token_source = ensure_token(hub.roots["data"])
    app.state.token = token
    app.state.token_source = token_source
    app.state.max_body_bytes = max_body_bytes()

    def denied(status: int, message: str, details: dict[str, Any] | None = None) -> JSONResponse:
        """拒绝响应只有一个形状：`{"error": {code, message, retryable, details}}`（标准第 (c) 条后半）。

        `details` 是一个**字典参数**，不是 `**kwargs`：两处调用点传的是 `{"host": …, "hint": …}`
        这样的一份整体详情，按关键字摊开会把调用方写好的 details 拆成函数的形参，
        当场 TypeError —— 守卫在最该说话的那一条分支上自己先炸掉。
        """
        return JSONResponse({"error": {"code": "permission_error" if status == 403 else
                                       ("input_error" if status == 413 else "permission_error"),
                                       "message": message, "retryable": False,
                                       "details": details or {}}}, status_code=status)

    @app.middleware("http")
    async def local_boundary(request: Request, call_next):
        """本机边界 + 共享口令 + 出口脱敏：一个点上判完，不指望每个路由自己记得。

        四道判据（对应共享验收标准 a–e，逐条解释写在 `local_guard.py` 的模块文档里）：
        ① Host 必须是字面量回环 + 本服务真实监听的端口（DNS rebinding 下 Host 就是那个
           恶意域名，直接拒 —— 拿 Origin 和请求自己的 Host 比才是这次的 bug）；
        ② Origin / Referer 存在时必须与 Host 同一个 host:port；
        ③ 非 GET 必须带共享口令（timing-safe 比较）：走 Cookie 的那条额外要求 Origin 相等，
           因为 Cookie 按域名共享而**不按端口**共享；
        ④ 响应出网前过 `redact_wire()`：命令行、日志、stdout 回声、存下来的 inputs 全遮。
        """
        host = request.headers.get("host", "")
        bound = _bound_port(request)
        problem = host_problem(host, bound_port=bound)
        if problem:
            return denied(403, f"这个服务只接受本机回环地址的调用：{problem}",
                          {"host": host, "hint": "要经反向代理访问请设 WORKFLOWHUB_ALLOWED_HOSTS"})
        origin = request.headers.get("origin", "")
        referer = request.headers.get("referer", "")
        for label, value in (("Origin", origin), ("Referer", referer)):
            bad = origin_problem(value, host) if value else ""
            if bad:
                return denied(403, f"跨源调用被拒（{label}）：{bad}",
                              {label.lower(): value, "hint": "只有本机浏览器里的界面与本机的 "
                                                              "X-WorkflowHub-Token 调用方可以写入"})
        if request.method.upper() in _WRITE_METHODS:
            declared = (request.headers.get("content-length") or "").strip()
            cap = app.state.max_body_bytes
            if declared.isdigit() and int(declared) > cap:
                return JSONResponse({"error": {"code": "input_error", "retryable": False,
                                               "message": f"请求体超过上限 {cap} 字节（收到 {declared}）",
                                               "details": {"max_body_bytes": cap}}},
                                    status_code=413)
            header_token = request.headers.get(TOKEN_HEADER, "")
            cookie_token = request.cookies.get(SESSION_COOKIE, "")
            if header_token:
                granted = token_matches(header_token, token)
            elif cookie_token:
                # 界面上没有地方填口令，所以口令由服务在已通过 ①② 的回环请求上发给它。
                # 这条路多要一条：Origin 必须逐字等于 Host —— 同机另一个端口的网页本来能
                # 白骑这个 Cookie（Cookie 不按端口共享），那一条正好把它挡掉。
                granted = (token_matches(cookie_token, token) and bool(origin)
                           and not origin_problem(origin, host))
            else:
                granted = False
            if not granted:
                return JSONResponse({"error": {"code": "permission_error", "retryable": False,
                                               "message": f"缺少 {TOKEN_HEADER}（本机共享口令）。"
                                                          "口令在 data/token.json 与 "
                                                          "data/agent-endpoint.json 里",
                                               "details": {"header": TOKEN_HEADER,
                                                           "token_file": str(token_path(hub.roots["data"]))}}},
                                    status_code=401)

        response = await call_next(request)
        # 回环且同源才发这把 Cookie：HttpOnly 让页面脚本读不到，SameSite=Strict 让别的站
        # 发起的请求根本带不上它 —— 口令因此只服务于"用户自己打开的那个界面"。
        carry = {SESSION_COOKIE: token} if request.method.upper() == "GET" else {}
        content_type = response.headers.get("content-type", "")
        if "application/json" not in content_type:
            for name, value in carry.items():
                response.set_cookie(name, value, httponly=True, samesite="strict", path="/")
            return response
        chunks = [chunk async for chunk in response.body_iterator]
        body = b"".join(chunks)
        try:
            payload = json.loads(body)
        except ValueError:
            for name, value in carry.items():
                response.set_cookie(name, value, httponly=True, samesite="strict", path="/")
            return response
        cleaned = json.dumps(redact_wire(payload), ensure_ascii=False).encode("utf-8")
        headers = dict(response.headers)
        headers.pop("content-length", None)
        out = Response(content=cleaned, status_code=response.status_code, headers=headers,
                       media_type=content_type)
        for name, value in carry.items():
            out.set_cookie(name, value, httponly=True, samesite="strict", path="/")
        return out

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
            "auth": {"required": True, "header": TOKEN_HEADER,
                     "file": "data/token.json（首次启动自动生成；本机调用方从这里读，网页读不到）",
                     "discovery": "data/agent-endpoint.json 与 data/desktop.json 里带同一把口令"},
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
        if "high_risk_enabled" in changed:
            # 注册表里放的是"当前可调用"的 Skill 快照，而高风险开关决定一批工具进不进得来。
            # 不刷新就会出这种形状：界面上开关明明打开了，调用却报 unknown Skill
            # （交付默认改成 False 之后这条路每天都走，以前是默认 True 才没人撞上）。
            hub.registry.refresh()
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
        report = services.list_workflows_report(hub)
        return {"items": report["items"], "skipped": report["skipped"],
                "note": "skipped 非空说明 data/workflows 里有读不出来的档：路径与原因都在里面，不是少一条就完了"}

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
        report = services.list_runs_report(hub, limit, status)
        return {"items": report["items"], "skipped": report["skipped"],
                "note": "skipped 非空说明 data/runs 里有坏 JSON：报路径与原因，不静默跳过"}

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
        # 围堵判据只有一份（`local_guard.require_within`）：/window/capture 那个写点
        # 走的是同一把尺，改一处两边都改，别再各写一遍。
        target = require_within(path, _readable_roots(hub), what="文件")
        if not target.is_file():
            raise not_found("文件", str(target))
        if download:
            return FileResponse(target, filename=target.name)
        limit = 200_000
        # 先按上限**读那么多**再判断有没有更多，而不是整份读进内存再切：
        # 界面会拿这个端点看日志，而 data/ 里躺着的可能是几百 MB 的 events.jsonl。
        with target.open("r", encoding="utf-8", errors="replace") as stream:
            text = stream.read(limit + 1)
        truncated = len(text) > limit
        return {"path": str(target), "size": target.stat().st_size,
                "text": text[:limit], "truncated": truncated}

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
        """把窗口截到 data/ 里的一个文件上 —— 路径围堵与 /api/v1/files 同一把尺。

        改之前这里是 `Path(path)` 直接交给 `window.capture()`，也就是"任意路径写"：
        配合当时那条宽松的跨源判据，一个网页就能往 `启动\\` 里放一个可执行文件
        （第 3 号 BLOCKER）。留 `path` 参数只为兼容界面里的"存到别处"入口，
        越界就当场 403 并且**一个字节都不写**。
        """
        controller = window()
        if controller is None:
            return {"ok": False, "reason": "非桌面模式"}
        target = (require_within(path, [hub.roots["data"]], what="截图") if path
                  else hub.roots["data"] / "window.png")
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


#: 模块级 `app` 改成按需构造（PEP 562）。
#:
#: 为什么不能在导入时就建：`create_app()` 会 `build_hub()` 到**真实的 data/**，而这一轮的
#: `create_app` 还要保证本机口令存在 —— 于是"任何人 import 一次 workflowhub.api"
#: （跑一遍测试就算）都会往用户的数据目录里写一把口令。规则写死了测试与导入都不许动真实
#: `data/`，所以这条路只能延迟到真有人要它的时候。
#: `python -m workflowhub serve` 与桌面端都按**对象**传 app（见 `__main__.cmd_serve` 里那条
#: 注释：字符串起法会让 uvicorn 再 import 一次模块、第二次 build_hub 真实目录），
#: 这里留着只为 `uvicorn workflowhub.api:app` 那种外部起法，用时再建，一个字节都不差。
_app_singleton: FastAPI | None = None


def __getattr__(name: str) -> Any:
    global _app_singleton
    if name != "app":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    if _app_singleton is None:
        _app_singleton = create_app()
    return _app_singleton
