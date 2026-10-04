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
#: 正在跑的调用：key = catalog_key，值 = 每一次现场（谁在跑、什么时候开始、哪种入口）
RUNNING: dict[str, list[dict[str, Any]]] = {}
#: 标了 exclusive 的工具排队跑；别的默认放开（allow_concurrent 是设置里的明说策略）
TOOL_LOCKS: dict[str, asyncio.Lock] = {}


def running_calls(catalog_key: str | None = None) -> list[dict[str, Any]]:
    """现在有几个调用在跑。界面与 /overview 用它区分"卡住"与"真的还在跑"。"""
    now = time.time()
    rows = []
    for key, entries in RUNNING.items():
        if catalog_key and key != catalog_key:
            continue
        for entry in entries:
            rows.append({**entry, "tool": key,
                         "elapsed_seconds": round(now - entry["_started"], 1)})
    return sorted(rows, key=lambda r: r["_started"])


def _tool_lock(key: str) -> asyncio.Lock:
    lock = TOOL_LOCKS.get(key)
    if lock is None:
        lock = TOOL_LOCKS.setdefault(key, asyncio.Lock())
    return lock


def _prune_tasks() -> None:
    cutoff = time.time() - TASK_TTL_SECONDS
    for key in [k for k, v in TASKS.items() if v.get("_finished", 0) and v["_finished"] < cutoff]:
        dropped = TASKS.pop(key, None) or {}
        handle = dropped.get("_handle")
        # 收尾保险：条记录过期了但协程还活着，就把它收掉，别让子进程在后台无人认领地跑
        if handle is not None and not handle.done():
            handle.cancel()


def resolve_call_timeout(inputs: dict[str, Any], default: float) -> float:
    """调用方写的 timeout_seconds 要么可用、要么当场说清为什么不行。

    以前这里 `float(...)` 不做范围检查，越界的值随后在 executor 里被 `min()` 悄悄夹住：
    Agent 要 7200s 实际得到 3600s，要 0s 实际得到默认值，谁都不知道 —— 判据只留一处，
    就是 `command_executor.check_timeout_range`。
    """
    from .command_executor import check_timeout_range

    raw = inputs.get("timeout_seconds")
    if raw in (None, ""):
        return float(default)
    return check_timeout_range(raw)


def call_plan(hub: Hub, catalog_key: str, inputs: dict[str, Any]) -> tuple[dict[str, Any], dict[str, ValueSpec], float]:
    """Split caller inputs into skill-declared fields plus call options, with a timeout."""
    if not isinstance(inputs, dict):
        raise WorkflowHubError(ErrorCode.INPUT_ERROR,
                               f"inputs 需要 JSON 对象，收到：{type(inputs).__name__}",
                               {"hint": '形如 {"argv": ["--version"], "dry_run": true}'})
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
    timeout = resolve_call_timeout(inputs, skill.timeout_seconds)
    return fields, declared, timeout


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


def _track_start(key: str, actor: str, source: str, timeout: float, dry_run: bool) -> dict[str, Any]:
    entry = {"actor": actor, "source": source, "dry_run": dry_run, "timeout_seconds": timeout,
             "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "_started": time.time(), "stage": "启动中"}
    RUNNING.setdefault(key, []).append(entry)
    return entry


def _track_stage(entry: dict[str, Any], stage: str) -> None:
    entry["stage"] = stage


def _track_finish(key: str, entry: dict[str, Any]) -> None:
    rows = RUNNING.get(key)
    if rows is None:
        return
    if entry in rows:
        rows.remove(entry)
    if not rows:
        RUNNING.pop(key, None)


async def invoke_tool(hub: Hub, catalog_key: str, inputs: dict[str, Any], actor: str = "agent",
                      source: str = "rest") -> dict[str, Any]:
    if not isinstance(inputs, dict):
        raise WorkflowHubError(ErrorCode.INPUT_ERROR,
                               f"inputs 需要 JSON 对象，收到：{type(inputs).__name__}",
                               {"hint": '形如 {"argv": ["--version"], "dry_run": true}'})
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
        hub.catalog.audit({"type": "blocked_pending_approval", "tool": catalog_key, "actor": actor,
                           "source": source})
        return {"status": "awaiting_approval", "approval": approval, "run_id": None,
                "hint": "审批模式已开启：请在 WorkflowHub 窗口批准，然后带 approved=true 重试"}

    fields, declared, timeout = call_plan(hub, catalog_key, inputs)
    names = tuple(name for name in ("exit_code", "stdout", "stderr", "command_line") if name in skill.outputs)
    workflow = single_tool_workflow(skill.id, skill.version, declared, fields, catalog_key,
                                    record.get("title", catalog_key), timeout, names)
    started = time.time()
    # 并发是一条明说的策略，不是一个巧合：默认放开，标了 exclusive 的工具排队。
    exclusive = bool(hub.catalog.state_for(catalog_key).get("exclusive")) and not inputs.get("dry_run")
    entry = _track_start(catalog_key, actor, source, timeout, bool(inputs.get("dry_run")))
    _track_stage(entry, "等待上一个调用" if exclusive else "执行中")
    try:
        if exclusive:
            async with _tool_lock(catalog_key):
                _track_stage(entry, "执行中")
                run = await hub.runner.run(workflow, fields)
        else:
            run = await hub.runner.run(workflow, fields)
    except asyncio.CancelledError:
        # 取消也要留痕：否则审计里只看得到"从没发生过"，而命令可能已经跑了一半。
        hub.catalog.audit({"type": "invoke", "tool": catalog_key, "actor": actor, "source": source,
                           "status": "cancelled", "dry_run": bool(inputs.get("dry_run")),
                           "duration_ms": int((time.time() - started) * 1000),
                           "note": "调用方取消或服务退出；子进程已按进程树收掉"})
        raise
    finally:
        _track_finish(catalog_key, entry)
    node_run = run.node_runs.get("call")
    result = node_run.result if node_run else None
    outputs = dict(result.outputs) if result else {}
    if result and result.error and run.error is None:
        run.error = result.error
    hub.catalog.audit({
        "type": "invoke", "tool": catalog_key, "actor": actor, "source": source, "run_id": run.id,
        "status": run.status.value, "exit_code": outputs.get("exit_code"),
        "command_line": outputs.get("command_line"), "dry_run": bool(inputs.get("dry_run")),
        "duration_ms": int((time.time() - started) * 1000),
        "timeout_seconds": outputs.get("timeout_seconds"),
        "cwd": outputs.get("cwd"),
        "error_code": (result.error or run.error).code.value if (result.error or run.error) else None,
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


def start_background_invoke(hub: Hub, catalog_key: str, inputs: dict[str, Any], actor: str,
                            source: str = "rest") -> dict[str, Any]:
    _prune_tasks()
    task_id = uuid4().hex[:12]
    TASKS[task_id] = {"id": task_id, "status": "running", "tool": catalog_key, "actor": actor,
                      "source": source,
                      "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                      "_started": time.time(), "stage": "已排队"}

    async def worker() -> None:
        try:
            TASKS[task_id]["stage"] = "执行中"
            result = await invoke_tool(hub, catalog_key, inputs, actor, source)
            TASKS[task_id]["result"] = result
            TASKS[task_id]["status"] = result.get("status", "succeeded")
            TASKS[task_id]["stage"] = result.get("status", "succeeded")
        except asyncio.CancelledError:
            TASKS[task_id]["status"] = "cancelled"
            TASKS[task_id]["stage"] = "已取消"
            TASKS[task_id]["error"] = ExecutionError(
                code=ErrorCode.PERMISSION_ERROR, message="调用已被取消",
                details={"hint": "子进程按进程树收掉了；要重跑请再发一次调用",
                         "tool": catalog_key, "actor": actor}).model_dump(mode="json")
            raise
        except WorkflowHubError as exc:
            TASKS[task_id]["status"] = "failed"
            TASKS[task_id]["stage"] = "失败"
            TASKS[task_id]["error"] = exc.detail.model_dump(mode="json")
        except Exception as exc:
            TASKS[task_id]["status"] = "failed"
            TASKS[task_id]["stage"] = "失败"
            TASKS[task_id]["error"] = ExecutionError(code=ErrorCode.UNKNOWN_ERROR,
                                                     message=f"{type(exc).__name__}: {exc}").model_dump(mode="json")
        TASKS[task_id]["_finished"] = time.time()

    TASKS[task_id]["_handle"] = asyncio.create_task(worker())
    return {"task_id": task_id, "status": "running", "poll": f"/api/v1/tasks/{task_id}",
            "tool": catalog_key, "cancel": f"/api/v1/tasks/{task_id}/cancel",
            "note": "后台任务在 WorkflowHub 退出后会丢；结果落在 data/runs/ 可回看"}


def cancel_task(task_id: str, actor: str = "ui") -> dict[str, Any] | None:
    """真的取消：掐协程 → CommandExecutor 收到 CancelledError → 按进程树杀子进程。

    返回 None 表示没有这个任务（由调用方给 404）。
    """
    task = TASKS.get(task_id)
    if task is None:
        return None
    if task.get("_finished"):
        return {**task_state(task_id or ""), "cancelled": False,
                "reason": f"任务已经结束（{task.get('status')}），取消太晚了"}
    handle = task.get("_handle")
    if handle is None or handle.done():
        task["status"] = "cancelled"
        return {**task_state(task_id), "cancelled": True}
    handle.cancel()
    task["status"] = "cancelling"
    task["stage"] = "取消中"
    return {**task_state(task_id), "cancelled": True, "actor": actor,
            "note": "已取消；子进程连同它自己派生的进程一起收掉"}


def task_state(task_id: str) -> dict[str, Any] | None:
    task = TASKS.get(task_id)
    if task is None:
        return None
    out = {k: v for k, v in task.items() if not k.startswith("_") and k != "_handle"}
    if task.get("_started"):
        out["elapsed_seconds"] = round(time.time() - task["_started"], 1)
    return out


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
