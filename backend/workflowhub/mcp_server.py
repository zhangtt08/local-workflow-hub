"""MCP (Model Context Protocol) server over STDIO, implemented directly on JSON-RPC 2.0.

Only eight meta-tools are advertised by default: a catalogue of over a thousand machine tools
does not belong in an Agent's context, so clients ``search_tools`` first and then ``invoke_tool``.
``--expose-tools`` opts into one MCP tool per enabled catalogue entry.
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from .bootstrap import build_hub
from .errors import WorkflowHubError
from .models import ErrorCode
from . import services

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "workflowhub", "version": "1.0.0",
               "description": "本机工具与 Workflow 编排：搜索→预览命令→执行→留痕"}

META_TOOLS: list[dict[str, Any]] = [
    {"name": "catalog_stats", "description": "目录概况：本机发现多少工具、分类、风险分布、上次扫描时间",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "search_tools", "description": "按关键词搜索本机工具（可执行文件/脚本/系统命令/应用/项目入口）",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "关键词，如 ffmpeg、下载、pdf"},
         "category": {"type": "string"}, "kind": {"type": "string",
             "description": "cli|script|app|project-script|skill|custom"},
         "risk": {"type": "string", "enum": ["low", "medium", "high"]},
         "limit": {"type": "integer", "minimum": 1, "maximum": 100}}, "required": ["query"]}},
    {"name": "get_tool", "description": "取一个工具的完整契约：参数、示例、命令行预览、最近审计",
     "inputSchema": {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]}},
    {"name": "invoke_tool", "description": "执行本机工具。先 dry_run=true 预览命令行，再正式执行",
     "inputSchema": {"type": "object", "properties": {
         "key": {"type": "string"},
         "argv": {"type": "array", "items": {"type": "string"}, "description": "按顺序追加的参数"},
         "flags": {"type": "object", "description": "具名参数（脚本类会按识别到的风格拼装）"},
         "stdin": {"type": "string"}, "cwd": {"type": "string"},
         "env": {"type": "object"}, "timeout_seconds": {"type": "number"},
         "dry_run": {"type": "boolean"}, "approved": {"type": "boolean"},
         "actor": {"type": "string", "description": "调用方标识，写入审计"}}, "required": ["key"]}},
    {"name": "list_workflows", "description": "列出已保存的 DAG workflow 及其版本、节点数",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "run_workflow", "description": "执行一个 workflow（id + version + inputs）",
     "inputSchema": {"type": "object", "properties": {
         "workflow_id": {"type": "string"}, "version": {"type": "integer"},
         "inputs": {"type": "object"}}, "required": ["workflow_id"]}},
    {"name": "get_run", "description": "读取一次运行的完整记录（节点、输入输出、错误、artifact）",
     "inputSchema": {"type": "object", "properties": {"run_id": {"type": "string"}},
                     "required": ["run_id"]}},
    {"name": "recent_activity", "description": "最近调用审计：谁调了什么、命令行、退出码",
     "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "maximum": 100}}}},
]


def _compact(rows: dict[str, Any]) -> dict[str, Any]:
    return {"total": rows["total"], "items": [
        {"key": r["id"], "title": r["title"], "kind": r["kind"], "category": r["category"],
         "risk": r["risk"], "enabled": r["enabled"], "description": r["description"][:200],
         "command": " ".join(r.get("command") or []), "args": [a.get("name") for a in (r.get("args") or [])][:12]}
        for r in rows["items"]]}


class Handler:
    def __init__(self, hub: Any, expose_all: bool = False):
        self.hub = hub
        self.expose_all = expose_all

    def tools_list(self) -> dict[str, Any]:
        tools = list(META_TOOLS)
        if self.expose_all:
            for skill in self.hub.catalog.enabled_skills():
                properties = {name: {"type": spec.type.value, "description": spec.description}
                              for name, spec in skill.inputs.items()}
                tools.append({"name": skill.id, "description": f"[{skill.risk_level.value}] {skill.description}"[:600],
                              "inputSchema": {"type": "object", "properties": properties,
                                              "required": [n for n, s in skill.inputs.items() if s.required]}})
        return {"tools": tools, "meta_tools": len(META_TOOLS),
                "instructions": "推荐流程：search_tools 找 key → get_tool 看参数 → invoke_tool(dry_run=true) 预览 → invoke_tool 执行。"}

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        hub = self.hub
        if name == "catalog_stats":
            return services.catalogue_stats(hub)
        if name == "search_tools":
            return _compact(hub.catalog.list(str(arguments.get("query", "")), arguments.get("category"),
                                              arguments.get("kind"), arguments.get("risk"), None, None,
                                              int(arguments.get("limit", 20))))
        if name == "get_tool":
            key = str(arguments["key"])
            if key not in hub.catalog.tools:
                raise WorkflowHubError(ErrorCode.VALIDATION_ERROR, f"未找到工具：{key}")
            skill = hub.catalog.to_skill(key)
            return {"tool": hub.catalog.record(key), "fields": {n: s.model_dump(mode="json")
                                                                for n, s in skill.inputs.items()},
                    "skill_id": skill.id, "command": " ".join((hub.catalog.tools[key].get("command") or [])),
                    "risk": skill.risk_level.value, "enabled": hub.catalog.is_enabled(key)}
        if name == "invoke_tool":
            payload = {k: v for k, v in arguments.items() if k not in {"key", "actor"}}
            return await services.invoke_tool(hub, str(arguments["key"]), payload,
                                              str(arguments.get("actor", "mcp-client")))
        if name == "list_workflows":
            return {"items": services.list_workflows(hub)}
        if name == "run_workflow":
            workflow = hub.workflows.get_version(str(arguments["workflow_id"]),
                                                 int(arguments.get("version", 1)))
            if workflow is None:
                raise WorkflowHubError(ErrorCode.VALIDATION_ERROR,
                                       f"未找到 workflow：{arguments['workflow_id']}@{arguments.get('version', 1)}")
            run = await hub.runner.run(workflow, arguments.get("inputs") or {})
            hub.catalog.audit({"type": "workflow_run", "tool": workflow.id, "actor": "mcp",
                               "run_id": run.id, "status": run.status.value})
            return run.model_dump(mode="json")
        if name == "get_run":
            run = hub.runs.get(str(arguments["run_id"]))
            if run is None:
                raise WorkflowHubError(ErrorCode.VALIDATION_ERROR, "未找到 run")
            return run.model_dump(mode="json")
        if name == "recent_activity":
            return {"items": hub.catalog.recent_activity(int(arguments.get("limit", 20)))}
        if self.expose_all:
            key = name
            if key in hub.catalog.tools:
                return await services.invoke_tool(hub, key, arguments, "mcp-client")
        raise WorkflowHubError(ErrorCode.VALIDATION_ERROR, f"未知工具：{name}")


def _reply(result: Any = None, error: dict[str, Any] | None = None, request_id: Any = None) -> None:
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        message["error"] = error
    else:
        message["result"] = result
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def serve_stdio(expose_all: bool = False) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
    hub = build_hub()
    if not hub.catalog.tools:
        hub.catalog.scan()
    hub.registry.refresh()
    handler = Handler(hub, expose_all)
    print(f"[workflowhub-mcp] 已就绪：{hub.catalog.facets()['total']} 个工具，STDIO 等待请求", file=sys.stderr)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            _reply(error={"code": -32700, "message": "Parse error"}, request_id=None)
            continue
        request_id = request.get("id")
        method = str(request.get("method", ""))
        params = request.get("params") or {}
        if method.startswith("notifications/"):
            continue
        if method == "initialize":
            _reply({"protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {**SERVER_INFO, "version": hub.version()}}, request_id=request_id)
            continue
        if method in ("ping", "logging/setLevel"):
            _reply({}, request_id=request_id)
            continue
        if method == "tools/list":
            _reply(handler.tools_list(), request_id=request_id)
            continue
        if method == "tools/call":
            name = str(params.get("name", ""))
            arguments = params.get("arguments") or {}
            try:
                result = asyncio.run(handler.call(name, arguments))
                _reply({"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, default=str)}],
                        "isError": False}, request_id=request_id)
            except WorkflowHubError as exc:
                payload = {"error": exc.detail.model_dump(mode="json")}
                _reply({"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
                        "isError": True}, request_id=request_id)
            except Exception as exc:
                payload = {"error": {"code": "unknown_error", "message": f"{type(exc).__name__}: {exc}"}}
                _reply({"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
                        "isError": True}, request_id=request_id)
            continue
        if method in ("resources/list", "prompts/list"):
            key = "resources" if method.startswith("resources") else "prompts"
            _reply({key: []}, request_id=request_id)
            continue
        _reply(error={"code": -32601, "message": f"Method not found: {method}"}, request_id=request_id)
    return 0
