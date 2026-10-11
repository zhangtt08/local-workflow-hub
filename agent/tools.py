from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
BACKEND = PROJECT_ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from workflowhub.bootstrap import build_hub
from workflowhub import services

PROJECT = {
    "id": "workflowhub",
    "name": "WorkflowHub",
    "version": "1.0.0",
    "summary": "本机工具与工作流编排中心：PATH可执行文件、脚本、项目入口与已安装应用统一注册，带契约搜索与调用。",
    "port": 8794,
}
PROJECT_ID = "workflowhub"

_hub_instance = None


def get_hub():
    global _hub_instance
    if _hub_instance is None:
        _hub_instance = build_hub()
    return _hub_instance


def handle_catalog_stats(args: dict[str, Any]) -> dict[str, Any]:
    hub = get_hub()
    return services.catalogue_stats(hub)


def handle_search_tools(args: dict[str, Any]) -> dict[str, Any]:
    hub = get_hub()
    query = str(args.get("query", ""))
    category = args.get("category")
    kind = args.get("kind")
    risk = args.get("risk")
    limit = int(args.get("limit", 20))
    rows = hub.catalog.list(query, category, kind, risk, None, None, limit)
    return {
        "total": rows["total"],
        "items": [
            {
                "key": r["id"],
                "title": r["title"],
                "kind": r["kind"],
                "category": r["category"],
                "risk": r["risk"],
                "enabled": r["enabled"],
                "description": r["description"][:200],
                "command": " ".join(r.get("command") or []),
            }
            for r in rows["items"]
        ],
    }


def handle_get_tool(args: dict[str, Any]) -> dict[str, Any]:
    hub = get_hub()
    key = str(args.get("key", ""))
    if key not in hub.catalog.tools:
        return {"error": f"未找到工具: {key}"}
    skill = hub.catalog.to_skill(key)
    return {
        "tool": hub.catalog.record(key),
        "skill_id": skill.id,
        "command": " ".join(hub.catalog.tools[key].get("command") or []),
        "risk": skill.risk_level.value,
        "enabled": hub.catalog.is_enabled(key),
    }


def handle_invoke_tool(args: dict[str, Any]) -> dict[str, Any]:
    hub = get_hub()
    key = str(args.get("key", ""))
    argv = args.get("argv", [])
    flags = args.get("flags", {})
    dry_run = bool(args.get("dry_run", False))
    return hub.executor.execute(key, argv=argv, flags=flags, dry_run=dry_run)


def handle_list_workflows(args: dict[str, Any]) -> dict[str, Any]:
    hub = get_hub()
    flows = hub.state.list_workflows()
    return {"workflows": [f.model_dump(mode="json") if hasattr(f, "model_dump") else f for f in flows]}


TOOLS = [
    {
        "name": "workflowhub.catalog_stats",
        "description": "目录概况：本机发现多少工具、分类分布、风险分布与上次扫描时间。",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
        "risk": "read",
        "handler": handle_catalog_stats,
    },
    {
        "name": "workflowhub.search_tools",
        "description": "按关键词搜索本机发现的可执行程序、脚本、系统工具与项目入口。",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "关键词，如 ffmpeg、git、curl 等"},
                "category": {"type": "string", "description": "分类过滤"},
                "kind": {"type": "string", "description": "类型: cli/script/app/project-script"},
                "risk": {"type": "string", "enum": ["low", "medium", "high"], "description": "风险等级"},
                "limit": {"type": "integer", "description": "返回条数上限"},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        "risk": "read",
        "handler": handle_search_tools,
    },
    {
        "name": "workflowhub.get_tool",
        "description": "获取某个本机工具的完整调用契约：命令、入参名、工作目录与风险。",
        "input_schema": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "工具标识 key"}
            },
            "required": ["key"],
            "additionalProperties": False,
        },
        "risk": "read",
        "handler": handle_get_tool,
    },
    {
        "name": "workflowhub.invoke_tool",
        "description": "通过 WorkflowHub 执行本机工具，建议先传 dry_run:true 进行命令预览。",
        "input_schema": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "工具 key"},
                "argv": {"type": "array", "description": "参数数组"},
                "flags": {"type": "object", "description": "具名标志参数"},
                "dry_run": {"type": "boolean", "description": "为 true 则只返回预览命令不真实执行"},
            },
            "required": ["key"],
            "additionalProperties": False,
        },
        "risk": "exec",
        "handler": handle_invoke_tool,
    },
    {
        "name": "workflowhub.list_workflows",
        "description": "列出已发布的工作流 (DAG) 及其版本与节点数。",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
        "risk": "read",
        "handler": handle_list_workflows,
    },
]
