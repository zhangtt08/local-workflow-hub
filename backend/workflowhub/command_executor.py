"""Executor for real machine commands discovered by the catalogue.

Commands run without a shell (``exec`` semantics), so Agent-supplied parameters cannot be
turned into shell injection. ``dry_run`` returns the exact command line before anything runs.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any
from uuid import uuid4

from .errors import WorkflowHubError
from .models import Artifact, ErrorCode, ExecutionError, NodeStatus, SkillDefinition, SkillResult, utc_now
from .ports import ExecutionContext

GENERIC_INPUTS = {"argv", "flags", "stdin", "cwd", "env", "timeout_seconds", "dry_run"}
MAX_ARGV_ITEMS = 128
MAX_ARG_CHARS = 4000


def arg_key(value: str) -> str:
    """Normalise a CLI parameter name to a comparable key: --Frontend-Url / FrontendUrl → frontend-url."""
    text = re.sub(r"^-+", "", str(value).strip())
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", text)
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _slug(value: str) -> str:
    return arg_key(value)


def _clean_sequence(values: Any, label: str) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple)):
        raise WorkflowHubError(ErrorCode.INPUT_ERROR, f"{label} 需要字符串数组")
    out: list[str] = []
    for item in values:
        text = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
        if "\x00" in text:
            raise WorkflowHubError(ErrorCode.INPUT_ERROR, f"{label} 含非法空字符")
        out.append(text[:MAX_ARG_CHARS])
    return out[:MAX_ARGV_ITEMS]


def _arg_specs(skill: SkillDefinition) -> list[dict[str, Any]]:
    raw = skill.metadata.get("arg_specs")
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def _match_spec(skill: SkillDefinition, field: str) -> dict[str, Any] | None:
    wanted = _slug(field)
    for spec in _arg_specs(skill):
        if _slug(str(spec.get("name") or "")) == wanted:
            return spec
    return None


def _flag(skill: SkillDefinition, field: str) -> str:
    spec = _match_spec(skill, field)
    name = str((spec or {}).get("name") or field)
    if name.startswith("-"):
        return name
    style = str(skill.metadata.get("flag_style") or "double-dash")
    return f"-{name}" if style == "single-dash" else f"--{name}"


def _render(flag: str, value: Any, boolean: bool) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for item in value:
            out.extend(_render(flag, item, boolean))
        return out
    if isinstance(value, bool):
        return [flag] if value else []
    if boolean:
        return [flag] if str(value).lower() in {"true", "1", "yes"} else []
    if isinstance(value, dict):
        value = json.dumps(value, ensure_ascii=False)
    return [flag, str(value)]


def build_command(skill: SkillDefinition, inputs: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    """Deterministic command line plus the options that wrap it."""
    base = [skill.executor.entry, *skill.executor.args]
    if not str(base[0] or "").strip():
        raise WorkflowHubError(ErrorCode.EXECUTOR_ERROR, "命令入口为空，无法执行")
    if not Path(str(base[0])).is_absolute():
        raise WorkflowHubError(ErrorCode.EXECUTOR_ERROR, f"命令必须登记为绝对路径：{base[0]}")

    tokens = list(base) + _clean_sequence(inputs.get("argv"), "argv")
    flags = inputs.get("flags") or {}
    if not isinstance(flags, dict):
        raise WorkflowHubError(ErrorCode.INPUT_ERROR, "flags 需要对象")
    provided = {**{k: v for k, v in inputs.items() if k not in GENERIC_INPUTS}, **flags}
    for field, value in provided.items():
        spec = _match_spec(skill, field)
        tokens.extend(_render(_flag(skill, field), value, bool(spec and spec.get("kind") == "boolean")))
    return tokens, {
        "cwd": inputs.get("cwd"), "env": inputs.get("env") or {}, "stdin": inputs.get("stdin"),
        "timeout": inputs.get("timeout_seconds"), "dry_run": bool(inputs.get("dry_run")),
    }


def command_line(tokens: list[str]) -> str:
    return " ".join(token if token and " " not in token else f'"{token}"' for token in tokens)


def decode_output(data: bytes) -> str:
    """Chinese Windows consoles answer in CP936 while most tools emit UTF-8; try both before mojibake."""
    if not data:
        return ""
    for encoding in ("utf-8", "gbk", "cp936"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


class CommandExecutor:
    """Runs a discovered machine command with bounded output and a hard timeout."""

    def __init__(self, max_output_bytes: int = 200_000):
        self.max_output_bytes = max_output_bytes

    async def execute(self, skill: SkillDefinition, inputs: dict[str, Any], context: ExecutionContext, timeout: float) -> SkillResult:
        started = utc_now()
        tokens, options = build_command(skill, inputs)
        line = command_line(tokens)
        effective_timeout = min(float(options["timeout"]) if options["timeout"] else timeout, skill.timeout_seconds, 3600)

        if options["dry_run"]:
            return SkillResult(
                status=NodeStatus.SUCCEEDED,
                outputs={"exit_code": None, "stdout": "", "stderr": "", "command_line": line,
                         "dry_run": True, "timeout_seconds": effective_timeout},
                logs=[f"dry_run: {line}"], started_at=started, finished_at=utc_now(),
            )

        extra_env = options["env"]
        if not isinstance(extra_env, dict):
            raise WorkflowHubError(ErrorCode.INPUT_ERROR, "env 需要对象")
        env = dict(os.environ)
        for key, value in extra_env.items():
            if not isinstance(key, str) or not key or "=" in key:
                raise WorkflowHubError(ErrorCode.INPUT_ERROR, f"非法环境变量名：{key!r}")
            env[key] = str(value)
        env.setdefault("PYTHONUTF8", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")

        cwd = options["cwd"] or skill.executor.cwd or str(Path(tokens[0]).parent)
        cwd_path = Path(cwd) if Path(cwd).is_dir() else Path.home()
        context.artifact_root.mkdir(parents=True, exist_ok=True)

        try:
            process = await asyncio.create_subprocess_exec(
                *tokens,
                stdin=asyncio.subprocess.PIPE if options["stdin"] else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                cwd=str(cwd_path), env=env,
            )
        except (OSError, ValueError) as exc:
            return SkillResult(
                status=NodeStatus.FAILED,
                error=ExecutionError(code=ErrorCode.EXECUTOR_ERROR, message=f"无法启动命令：{exc}",
                                     details={"command_line": line, "cwd": str(cwd_path)}),
                logs=[line], started_at=started, finished_at=utc_now(),
            )

        stdin_bytes = str(options["stdin"]).encode("utf-8") if options["stdin"] else None
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(stdin_bytes), timeout=effective_timeout)
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()
            return SkillResult(
                status=NodeStatus.FAILED, logs=[line],
                error=ExecutionError(code=ErrorCode.TIMEOUT, retryable=True,
                                     message=f"命令超过 {effective_timeout:g}s 未结束，已强制终止",
                                     details={"command_line": line}),
                started_at=started, finished_at=utc_now(),
            )

        cap = self.max_output_bytes
        raw_out = decode_output(stdout)
        raw_err = decode_output(stderr)
        out_text, err_text = raw_out[:cap], raw_err[:cap]
        artifacts = []
        for label, text in (("stdout", out_text), ("stderr", err_text)):
            if not text.strip():
                continue
            path = context.artifact_root / f"{label}-{uuid4().hex[:8]}.txt"
            path.write_text(text, encoding="utf-8")
            artifacts.append(Artifact(id=uuid4().hex, uri=str(path), media_type="text/plain",
                                      node_run_id=context.node_run_id, size_bytes=path.stat().st_size))
        exit_code = process.returncode
        outputs = {
            "exit_code": exit_code, "stdout": out_text, "stderr": err_text, "command_line": line,
            "cwd": str(cwd_path), "duration_ms": int((utc_now() - started).total_seconds() * 1000),
            "truncated": len(raw_out) > cap or len(raw_err) > cap,
        }
        logs = [line, *out_text.splitlines()[:200]]
        if exit_code not in (0, None):
            return SkillResult(
                status=NodeStatus.FAILED, outputs=outputs, artifacts=artifacts, logs=logs,
                error=ExecutionError(code=ErrorCode.EXECUTOR_ERROR, retryable=False,
                                     message=f"命令退出码 {exit_code}",
                                     details={"command_line": line, "stderr": err_text[:800]}),
                started_at=started, finished_at=utc_now(),
            )
        return SkillResult(status=NodeStatus.SUCCEEDED, outputs=outputs, artifacts=artifacts,
                           logs=logs, started_at=started, finished_at=utc_now())
