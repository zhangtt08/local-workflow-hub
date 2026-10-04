"""Executor routing and a bounded local Python subprocess adapter."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

from .errors import WorkflowHubError
from .models import Artifact, ErrorCode, ExecutionError, NodeStatus, SkillDefinition, SkillResult, utc_now
from .ports import ExecutionContext, SkillExecutor


class ExecutorRouter:
    def __init__(self):
        self._executors: dict[str, SkillExecutor] = {}

    def register(self, executor_type: str, executor: SkillExecutor) -> None:
        self._executors[executor_type] = executor

    def get(self, executor_type: str) -> SkillExecutor:
        try:
            return self._executors[executor_type]
        except KeyError as exc:
            raise WorkflowHubError(ErrorCode.EXECUTOR_ERROR, f"No executor: {executor_type}") from exc

    @property
    def types(self) -> set[str]:
        return set(self._executors)


class PythonExecutor:
    """JSON stdin/stdout protocol. Timeout kills the process; this is not a sandbox."""

    def __init__(self, skill_root: Path):
        self.skill_root = skill_root.resolve()

    async def execute(self, skill: SkillDefinition, inputs: dict[str, Any], context: ExecutionContext, timeout: float) -> SkillResult:
        started = utc_now()
        base_folder = (self.skill_root / skill.id).resolve()
        version_folder = (base_folder / "versions" / skill.version).resolve()
        folder = version_folder if (version_folder / "skill.yaml").is_file() else base_folder
        entry = (folder / skill.executor.entry).resolve()
        valid_folder = base_folder.parent == self.skill_root and (folder == base_folder or folder == base_folder / "versions" / skill.version)
        if not valid_folder or not entry.is_relative_to(folder) or not entry.is_file():
            raise WorkflowHubError(ErrorCode.EXECUTOR_ERROR, "Skill entry escapes its directory or is missing")
        context.artifact_root.mkdir(parents=True, exist_ok=True)
        env = {key: os.environ[key] for key in ("SystemRoot", "WINDIR", "PATH", "TEMP", "TMP") if key in os.environ}
        env.update({"PYTHONUTF8": "1", "WORKFLOWHUB_ARTIFACT_ROOT": str(context.artifact_root)})
        process = await asyncio.create_subprocess_exec(
            sys.executable, str(entry),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=str(folder), env=env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(json.dumps(inputs, ensure_ascii=False).encode("utf-8")), timeout=timeout)
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()
            return SkillResult(status=NodeStatus.FAILED, error=ExecutionError(code=ErrorCode.TIMEOUT, message=f"Skill exceeded {timeout:g}s", retryable=True), started_at=started, finished_at=utc_now())
        from .command_executor import decode_output

        logs = decode_output(stderr).splitlines()[:100]
        try:
            payload = json.loads(decode_output(stdout))
            if not isinstance(payload, dict):
                raise ValueError("Result must be a JSON object")
        except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
            return SkillResult(status=NodeStatus.FAILED, logs=logs, error=ExecutionError(code=ErrorCode.EXECUTOR_ERROR, message=f"Invalid executor response: {exc}"), started_at=started, finished_at=utc_now())
        if process.returncode != 0 or "error" in payload:
            raw_error = payload.get("error", {})
            if not isinstance(raw_error, dict):
                raw_error = {}
            try:
                code = ErrorCode(raw_error.get("code", ErrorCode.EXECUTOR_ERROR))
            except ValueError:
                code = ErrorCode.EXECUTOR_ERROR
            return SkillResult(status=NodeStatus.FAILED, logs=logs, error=ExecutionError(code=code, message=str(raw_error.get("message", f"Process exited {process.returncode}")), retryable=bool(raw_error.get("retryable", False))), started_at=started, finished_at=utc_now())
        outputs = payload.get("outputs", {})
        if not isinstance(outputs, dict):
            return SkillResult(status=NodeStatus.FAILED, error=ExecutionError(code=ErrorCode.OUTPUT_VALIDATION_ERROR, message="outputs must be an object"), started_at=started, finished_at=utc_now())
        artifacts = []
        for raw in payload.get("artifacts", []):
            path = Path(raw["uri"]).resolve()
            if not path.is_relative_to(context.artifact_root.resolve()) or not path.is_file():
                return SkillResult(status=NodeStatus.FAILED, error=ExecutionError(code=ErrorCode.OUTPUT_VALIDATION_ERROR, message="Artifact must be an existing file under artifact root"), started_at=started, finished_at=utc_now())
            artifacts.append(Artifact(id=uuid4().hex, uri=str(path), media_type=raw.get("media_type", "application/octet-stream"), node_run_id=context.node_run_id, size_bytes=path.stat().st_size))
        return SkillResult(status=NodeStatus.SUCCEEDED, outputs=outputs, artifacts=artifacts, logs=logs, started_at=started, finished_at=utc_now())
