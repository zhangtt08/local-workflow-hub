"""Application services shared by the REST API, the MCP server and the CLI.

One tool call is expressed as a one-node Workflow so agents, the UI and hand-built DAGs all
travel the same validate → snapshot → execute → trace path.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .bootstrap import Hub, static_root
from .errors import WorkflowHubError
from .models import (
    BindingType,
    ErrorCode,
    ExecutionError,
    InputBinding,
    NodeType,
    ValueSpec,
    ValueType,
    WorkflowDefinition,
    WorkflowNode,
)

GENERIC_INPUTS = {"argv", "flags", "stdin", "cwd", "env", "timeout_seconds", "dry_run", "approved"}
TASKS: dict[str, dict[str, Any]] = {}
TASK_TTL_SECONDS = 1800


def _prune_tasks() -> None:
    cutoff = time.time() - TASK_TTL_SECONDS
    for key in [k for k, v in TASKS.items() if v.get("_finished", 0) and v["_finished"] < cutoff]:
        TASKS.pop(key, None)


def call_plan(hub: Hub, catalog_key: str, inputs: dict[str, Any]) -> tuple[dict[str, Any], dict[str, ValueSpec], float]:
    """Split caller inputs into skill-declared fields plus call options, with a timeout."""
    skill = hub.catalog.to_skill(catalog_key)
    declared = dict(skill.inputs)
    raw_flags = inputs.get("flags") or {}
    if raw_flags and not isinstance(raw_flags, dict):
        raise WorkflowHubError(ErrorCode.INPUT_ERROR, "flags 需要对象")
    fields: dict[str, Any] = {}
    for key, value in inputs.items():
        if key == "flags":
            continue
        if key in declared:
            fields[key] = value
        elif key not in GENERIC_INPUTS:
            resolved = _match_field(declared, key)
            if resolved:
                fields[resolved] = value
    for key, value in raw_flags.items():
        token = key if key in declared else _match_field(declared, str(key))
        if token is None:
            raise WorkflowHubError(ErrorCode.INPUT_ERROR,
                                  f"该工具没有参数 {key}", {"unknown": key, "declared": sorted(declared)})
        fields[token] = value
    unknown = sorted(k for k in inputs
                     if k not in GENERIC_INPUTS and k not in declared and _match_field(declared, k) is None)
    if unknown:
        raise WorkflowHubError(ErrorCode.INPUT_ERROR,
                               f"未声明的输入：{', '.join(unknown)}",
                               {"unknown": unknown, "declared": sorted(declared),
                                "options": sorted(GENERIC_INPUTS - {"approved"})})
    timeout = float(inputs.get("timeout_seconds") or skill.timeout_seconds)
    return fields, declared, min(timeout, 3600.0)


def _match_field(declared: dict[str, ValueSpec], raw_name: str) -> str | None:
    from .command_executor import arg_key

    target = arg_key(raw_name)
    if target in declared:
        return target
    for name in declared:
        if arg_key(name) == target:
            return name
    return None


def single_tool_workflow(skill_id: str, skill_version: str, declared: dict[str, ValueSpec],
                         fields: dict[str, Any], catalog_key: str, title: str, timeout: float,
                         output_names: tuple[str, ...] = ("exit_code", "stdout", "stderr", "command_line")) -> WorkflowDefinition:
    bindings = {name: InputBinding(type=BindingType.WORKFLOW_INPUT, name=name) for name in fields}
    workflow_inputs = {
        name: ValueSpec(type=ValueType.ANY, required=name in fields and spec.required,
                       description=spec.description)
        for name, spec in declared.items() if name in fields
    }
    node = WorkflowNode(id="call", type=NodeType.SKILL, skill_id=skill_id, skill_version=skill_version,
                        inputs=bindings, timeout_seconds=timeout)
    return WorkflowDefinition(
        id=f"call-{skill_id}", name=f"单次调用 · {title}", version=1,
        inputs=workflow_inputs, nodes=[node], edges=[],
        outputs={name: InputBinding(type=BindingType.NODE_OUTPUT, node_id="call", field=name)
                 for name in output_names},
        metadata={"catalog_key": catalog_key, "kind": "single-tool"},
    )


async def invoke_tool(hub: Hub, catalog_key: str, inputs: dict[str, Any], actor: str = "agent") -> dict[str, Any]:
    if catalog_key not in hub.catalog.tools:
        raise WorkflowHubError(ErrorCode.INPUT_ERROR, f"未知工具：{catalog_key}", {"catalog_key": catalog_key})
    record = hub.catalog.record(catalog_key) or {}
    skill = hub.catalog.to_skill(catalog_key)
    if not hub.catalog.is_enabled(catalog_key):
        raise WorkflowHubError(ErrorCode.PERMISSION_ERROR,
                               f"工具「{record.get('title', catalog_key)}」未启用",
                               {"enabled": False, "risk": skill.risk_level.value,
                                "hint": "在工具库页面启用，或检查设置中的高风险工具开关"})
    if hub.catalog.needs_approval(catalog_key) and not inputs.get("approved"):
        approval = hub.catalog.create_approval(catalog_key, inputs, actor)
        hub.catalog.audit({"type": "blocked_pending_approval", "tool": catalog_key, "actor": actor})
        return {"status": "awaiting_approval", "approval": approval, "run_id": None,
                "hint": "审批模式已开启：请在 WorkflowHub 窗口批准，然后带 approved=true 重试"}

    fields, declared, timeout = call_plan(hub, catalog_key, inputs)
    names = tuple(name for name in ("exit_code", "stdout", "stderr", "command_line") if name in skill.outputs)
    workflow = single_tool_workflow(skill.id, skill.version, declared, fields, catalog_key,
                                    record.get("title", catalog_key), timeout, names)
    started = time.time()
    run = await hub.runner.run(workflow, fields)
    node_run = run.node_runs.get("call")
    result = node_run.result if node_run else None
    outputs = dict(result.outputs) if result else {}
    if result and result.error and run.error is None:
        run.error = result.error
    hub.catalog.audit({
        "type": "invoke", "tool": catalog_key, "actor": actor, "run_id": run.id,
        "status": run.status.value, "exit_code": outputs.get("exit_code"),
        "command_line": outputs.get("command_line"), "dry_run": bool(inputs.get("dry_run")),
        "duration_ms": int((time.time() - started) * 1000),
    })
    return {
        "status": run.status.value, "run_id": run.id, "trace_id": run.trace_id,
        "tool": {"key": catalog_key, "skill_id": skill.id, "title": record.get("title"),
                 "risk": record.get("risk")},
        "outputs": outputs,
        "artifacts": [a.model_dump(mode="json") for a in (result.artifacts if result else [])],
        "logs": result.logs[:120] if result else [],
        "error": (result.error or run.error).model_dump(mode="json") if result and ((result.error or run.error)) else None,
    }


def start_background_invoke(hub: Hub, catalog_key: str, inputs: dict[str, Any], actor: str) -> dict[str, Any]:
    _prune_tasks()
    task_id = uuid4().hex[:12]
    TASKS[task_id] = {"id": task_id, "status": "running", "tool": catalog_key, "actor": actor,
                      "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}

    async def worker() -> None:
        try:
            result = await invoke_tool(hub, catalog_key, inputs, actor)
            TASKS[task_id]["result"] = result
            TASKS[task_id]["status"] = result.get("status", "succeeded")
        except WorkflowHubError as exc:
            TASKS[task_id]["status"] = "failed"
            TASKS[task_id]["error"] = exc.detail.model_dump(mode="json")
        except Exception as exc:
            TASKS[task_id]["status"] = "failed"
            TASKS[task_id]["error"] = ExecutionError(code=ErrorCode.UNKNOWN_ERROR,
                                                     message=f"{type(exc).__name__}: {exc}").model_dump(mode="json")
        TASKS[task_id]["_finished"] = time.time()

    asyncio.create_task(worker())
    return {"task_id": task_id, "status": "running", "poll": f"/api/v1/tasks/{task_id}",
            "tool": catalog_key}


def task_state(task_id: str) -> dict[str, Any] | None:
    task = TASKS.get(task_id)
    if task is None:
        return None
    return {k: v for k, v in task.items() if not k.startswith("_")}


def snippets(base_url: str, catalog_key: str, skill_id: str, inputs: dict[str, Any]) -> dict[str, str]:
    """Copy-paste call examples for the ways an Agent consumes a tool."""
    body = json.dumps({"inputs": inputs}, ensure_ascii=False)
    schema = {
        "type": "function",
        "function": {
            "name": skill_id.replace("-", "_"),
            "description": f"调用本机工具 {catalog_key}",
            "parameters": {"type": "object", "properties": {k: {"type": "string"} for k in inputs},
                           "required": [k for k, v in inputs.items() if v not in (None, "", [])]},
        },
    }
    return {
        "curl": f"curl -s -X POST {base_url}/api/v1/tools/{catalog_key}/invoke -H \"Content-Type: application/json\" -d \"{body}\"",
        "python": ("import requests\n\n"
                   f"r = requests.post('{base_url}/api/v1/tools/{catalog_key}/invoke', json={body})\n"
                   "data = r.json()\nprint(data['status'], data['outputs'].get('stdout'))"),
        "function_calling": json.dumps(schema, ensure_ascii=False, indent=2),
        "mcp": json.dumps({"mcpServers": {"workflowhub": {"command": "python",
                                                          "args": ["-m", "workflowhub", "mcp"],
                                                          "cwd": "<项目目录>/backend"}}},
                          ensure_ascii=False, indent=2),
    }


def overview(hub: Hub) -> dict[str, Any]:
    facets = hub.catalog.facets()
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "catalog": facets,
        "last_scan": hub.catalog.last_scan,
        "scan_state": hub.catalog.scan_state,
        "settings": hub.catalog.settings,
        "skills": {"count": len(hub.directory.list()), "items": [s.id for s in hub.directory.list()][:20]},
        "workflows": {"count": len(list_workflows(hub)), "items": list_workflows(hub)[:10]},
        "activity": hub.catalog.recent_activity(14),
        "recent_runs": list_runs(hub, limit=10),
        "approvals": hub.catalog.pending_approvals(),
        "service": {"static_ui": bool(static_root()), "python": f"{Path(__file__).parts[0] and ''}"
                    f"{__import__('sys').version.split()[0]}", "version": hub.version()},
    }


def list_workflows(hub: Hub) -> list[dict[str, Any]]:
    root = hub.roots["data"] / "workflows"
    out: list[dict[str, Any]] = []
    if not root.is_dir():
        return out
    for folder in sorted(root.iterdir()):
        for version_file in sorted(folder.glob("*.json")):
            try:
                workflow = WorkflowDefinition.model_validate_json(version_file.read_text(encoding="utf-8"))
            except Exception:
                continue
            out.append({"id": workflow.id, "name": workflow.name, "version": workflow.version,
                        "nodes": len(workflow.nodes), "edges": len(workflow.edges),
                        "inputs": sorted(workflow.inputs), "file": str(version_file)})
    return sorted(out, key=lambda item: item["id"])


def list_runs(hub: Hub, limit: int = 25, status: str | None = None) -> list[dict[str, Any]]:
    root = hub.roots["data"] / "runs"
    if not root.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    files = sorted(root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in files[: max(limit, 1) * 4]:
        summary = _run_summary(path)
        if summary:
            rows.append(summary)
    if status:
        rows = [r for r in rows if r["status"] == status]
    return rows[:limit]


def _run_summary(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None
    nodes = data.get("node_runs", {}) or {}
    return {
        "id": data.get("id"), "trace_id": data.get("trace_id"),
        "workflow_id": data.get("workflow_id"), "workflow_version_id": data.get("workflow_version_id"),
        "status": data.get("status"), "started_at": data.get("started_at"), "finished_at": data.get("finished_at"),
        "kind": "single-tool" if str(data.get("workflow_id", "")).startswith("call-") else "workflow",
        "catalog_key": (data.get("definition_snapshot", {}) or {}).get("metadata", {}).get("catalog_key"),
        "nodes": [{"id": nid, "skill": n.get("skill_id"), "status": n.get("status"), "attempt": n.get("attempt"),
                   "error": ((n.get("result") or {}).get("error") or {}).get("message")}
                  for nid, n in nodes.items()],
        "inputs": data.get("inputs"), "outputs": data.get("outputs"),
        "error": (data.get("error") or {}).get("message"),
    }


def catalogue_stats(hub: Hub) -> dict[str, Any]:
    facets = hub.catalog.facets()
    return {"total": facets["total"], "enabled": facets["enabled"], "starred": facets["starred"],
            "categories": facets["categories"], "kinds": facets["kinds"], "risks": facets["risks"],
            "last_scan": hub.catalog.last_scan, "scan_state": hub.catalog.scan_state,
            "approval_mode": hub.catalog.settings["approval_mode"],
            "high_risk_enabled": hub.catalog.settings["high_risk_enabled"],
            "skills": len(hub.directory.list()), "workflows": len(list_workflows(hub)),
            "pending_approvals": len(hub.catalog.pending_approvals())}


def resolve_skill_id(hub: Hub, catalog_key: str) -> str:
    return hub.catalog.to_skill(catalog_key).id
