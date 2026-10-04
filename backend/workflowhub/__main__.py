"""WorkflowHub command line.

``serve`` runs the HTTP API, ``desktop`` runs the app window, ``mcp`` speaks MCP over stdio,
and the scan/invoke/tools verbs are what an Agent shells out to when it has no HTTP client.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .bootstrap import build_hub, data_root, static_root
from .errors import WorkflowHubError


def _print(payload: Any) -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    os.environ.setdefault("WORKFLOWHUB_SERVE", "1")
    uvicorn.run("workflowhub.api:app", host=args.host, port=args.port, reload=False, log_level="info")
    return 0


def cmd_desktop(args: argparse.Namespace) -> int:
    from .desktop.launcher import run_desktop

    return run_desktop(port=args.port, start=args.start, url=args.url, browser=args.browser,
                       headless_check=args.check)


def cmd_scan(args: argparse.Namespace) -> int:
    hub = build_hub()
    roots = [str(Path(r)) for r in args.roots] if args.roots else None
    result = hub.catalog.scan(roots)
    hub.registry.refresh()
    _print({"scan": result, "stats": hub.catalog.facets(), "errors": hub.catalog.scan_errors})
    return 0


def cmd_tools(args: argparse.Namespace) -> int:
    hub = build_hub()
    if not hub.catalog.tools:
        hub.catalog.scan()
    rows = hub.catalog.list(args.query, args.category, args.kind, limit=args.limit)
    _print({"total": rows["total"], "items": [
        {"key": r["id"], "title": r["title"], "kind": r["kind"], "category": r["category"],
         "risk": r["risk"], "enabled": r["enabled"], "description": r["description"][:120],
         "command": " ".join(r.get("command") or [])} for r in rows["items"]]})
    return 0


def cmd_invoke(args: argparse.Namespace) -> int:
    hub = build_hub()
    if not hub.catalog.tools:
        hub.catalog.scan()
    inputs: dict[str, Any] = {}
    if args.json:
        inputs = json.loads(args.json)
    if args.argv:
        inputs["argv"] = args.argv
    if args.dry_run:
        inputs["dry_run"] = True
    result = asyncio.run(_invoke(hub, args.key, inputs, args.actor))
    _print(result)
    return 0 if result.get("status") in {"succeeded", "awaiting_approval"} else 1


async def _invoke(hub: Any, key: str, inputs: dict[str, Any], actor: str) -> dict[str, Any]:
    from . import services

    return await services.invoke_tool(hub, key, inputs, actor)


def cmd_mcp(args: argparse.Namespace) -> int:
    from .mcp_server import serve_stdio

    return serve_stdio(expose_all=args.expose_tools)


def cmd_doctor(args: argparse.Namespace) -> int:
    report: dict[str, Any] = {"python": sys.version.split()[0], "executable": sys.executable,
                              "frozen": bool(getattr(sys, "frozen", False))}
    for module in ("fastapi", "uvicorn", "pydantic", "yaml", "PIL"):
        try:
            __import__(module)
            report[f"dep:{module}"] = "ok"
        except Exception as exc:
            report[f"dep:{module}"] = f"missing ({exc})"
    root = data_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        report["data_root"] = f"{root} (可写)"
    except Exception as exc:
        report["data_root"] = f"{root} (不可写: {exc})"
    ui = static_root()
    report["ui"] = str(ui) if ui else "未构建：frontend 下执行 npm install && npm run build"
    from .desktop.launcher import browser_candidates

    report["browsers"] = browser_candidates() or ["未找到 Edge/Chromium，只能用浏览器访问 UI"]
    try:
        hub = build_hub()
        report["catalog"] = hub.catalog.facets()
        report["skills"] = len(hub.directory.list())
        report["workflows"] = len(__import__("workflowhub.services", fromlist=["list_workflows"]).list_workflows(hub))
        report["executors"] = sorted(hub.executors.types)
    except Exception as exc:
        report["hub_error"] = f"{type(exc).__name__}: {exc}"
    _print(report)
    return 0


def cmd_workflows(args: argparse.Namespace) -> int:
    from . import services

    hub = build_hub()
    if args.action == "list":
        _print({"items": services.list_workflows(hub)})
        return 0
    if args.action == "run":
        run = asyncio.run(hub.runner.run(hub.workflows.get_version(args.workflow_id, args.version),
                                         json.loads(args.inputs or "{}")))
        hub.catalog.audit({"type": "workflow_run", "tool": args.workflow_id, "actor": "cli",
                           "run_id": run.id, "status": run.status.value})
        _print(run.model_dump(mode="json"))
        return 0
    _print({"error": "用法：workflows list | run <id> --version N --inputs '{...}'"})
    return 2


def cmd_runs(args: argparse.Namespace) -> int:
    from . import services

    hub = build_hub()
    if args.run_id:
        run = hub.runs.get(args.run_id)
        _print(run.model_dump(mode="json") if run else {"error": f"未找到 run {args.run_id}"})
        return 0
    _print({"items": services.list_runs(hub, args.limit)})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="workflowhub", description="WorkflowHub · 本机工具与 Workflow 编排中心")
    parser.add_argument("--mcp", action="store_true", help="以 MCP STDIO 模式启动（供桌面 exe 直接传入）")
    parser.add_argument("--version", action="version", version=f"workflowhub {__version__}")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="只启动 HTTP 服务")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8794)
    serve.set_defaults(func=cmd_serve)

    desktop = sub.add_parser("desktop", help="启动桌面窗口（无边框，窗口控件在界面内）")
    desktop.add_argument("--port", type=int, default=None)
    desktop.add_argument("--start", choices=["maximized", "windowed"], default="maximized")
    desktop.add_argument("--url", default=None, help="自定义界面地址（开发时指向 Vite）")
    desktop.add_argument("--browser", default=None, help="指定 Chromium/Edge 可执行文件")
    desktop.add_argument("--check", action="store_true", help="只启动服务并打印地址，不开窗口")
    desktop.set_defaults(func=cmd_desktop)

    scan = sub.add_parser("scan", help="扫描本机工具")
    scan.add_argument("--roots", nargs="*", default=None)
    scan.set_defaults(func=cmd_scan)

    tools = sub.add_parser("tools", help="列出/搜索工具目录")
    tools.add_argument("query", nargs="?", default="")
    tools.add_argument("--category", default=None)
    tools.add_argument("--kind", default=None)
    tools.add_argument("--limit", type=int, default=25)
    tools.set_defaults(func=cmd_tools)

    invoke = sub.add_parser("invoke", help="调用一个工具（与 HTTP/MCP 同一链路）")
    invoke.add_argument("key")
    invoke.add_argument("--argv", nargs="*", default=None)
    invoke.add_argument("--json", default=None, help="完整 inputs 对象 JSON")
    invoke.add_argument("--actor", default="cli")
    invoke.add_argument("--dry-run", action="store_true")
    invoke.set_defaults(func=cmd_invoke)

    mcp = sub.add_parser("mcp", help="MCP STDIO 服务")
    mcp.add_argument("--expose-tools", action="store_true",
                     help="把每个已启用工具单独暴露为 MCP 工具（清单会很大）")
    mcp.set_defaults(func=cmd_mcp)
    sub.add_parser("doctor", help="自检运行环境").set_defaults(func=cmd_doctor)

    workflows = sub.add_parser("workflows", help="workflow 列表/执行")
    workflows.add_argument("action", choices=["list", "run"])
    workflows.add_argument("workflow_id", nargs="?", default="")
    workflows.add_argument("--version", type=int, default=1)
    workflows.add_argument("--inputs", default=None)
    workflows.set_defaults(func=cmd_workflows)

    runs = sub.add_parser("runs", help="运行记录")
    runs.add_argument("run_id", nargs="?", default=None)
    runs.add_argument("--limit", type=int, default=20)
    runs.set_defaults(func=cmd_runs)
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    os.environ.setdefault("PYTHONUTF8", "1")
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--mcp" in argv:
        from .mcp_server import serve_stdio

        return serve_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        args = parser.parse_args(["doctor"] + argv)
    try:
        return int(args.func(args) or 0)
    except WorkflowHubError as exc:
        _print({"error": exc.detail.model_dump(mode="json")})
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
