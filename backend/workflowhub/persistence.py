"""Small durable adapters; SQL schema documents the planned relational boundary."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from .errors import WorkflowHubError
from .models import ErrorCode, WorkflowDefinition, WorkflowRun

#: Windows rename 会被瞬时共享锁挡住（杀软、索引器、另一进程刚打开过同名文件）
_REPLACE_SCHEDULE = (0.0, 0.05, 0.15, 0.35, 0.7)


def atomic_replace(tmp: Path, target: Path, schedule: tuple[float, ...] = _REPLACE_SCHEDULE) -> None:
    """命令已经跑完了，落盘失败不能把它记成执行失败 —— 所以先重试，再如实报错。"""
    for pause in schedule:
        if pause:
            time.sleep(pause)
        try:
            tmp.replace(target)
            return
        except PermissionError:
            continue
    raise WorkflowHubError(
        ErrorCode.EXECUTOR_ERROR,
        f"结果写入失败：{target.name} 一直被占用（临时文件保留为 {tmp.name}）",
        {"path": str(target), "retryable": True},
        retryable=True,
    )


def _safe_key(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("Identifier cannot be used as a storage path")
    return value


class FileWorkflowRepository:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, workflow_id: str, version: int) -> Path:
        return self.root / _safe_key(workflow_id) / f"{version}.json"

    def save_version(self, workflow: WorkflowDefinition) -> None:
        path = self._path(workflow.id, workflow.version)
        path.parent.mkdir(parents=True, exist_ok=True)
        content = workflow.model_dump_json(indent=2)
        try:
            with path.open("x", encoding="utf-8") as stream:
                stream.write(content)
            return
        except FileExistsError:
            existing = WorkflowDefinition.model_validate_json(path.read_text(encoding="utf-8"))
            if existing != workflow:
                raise ValueError("Published Workflow version is immutable")

    def get_version(self, workflow_id: str, version: int) -> WorkflowDefinition | None:
        path = self._path(workflow_id, version)
        return WorkflowDefinition.model_validate_json(path.read_text(encoding="utf-8")) if path.exists() else None


class FileRunRepository:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, run: WorkflowRun) -> None:
        path = self.root / f"{_safe_key(run.id)}.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(run.model_dump_json(indent=2), encoding="utf-8")
        atomic_replace(temporary, path)

    def get(self, run_id: str) -> WorkflowRun | None:
        path = self.root / f"{_safe_key(run_id)}.json"
        return WorkflowRun.model_validate_json(path.read_text(encoding="utf-8")) if path.exists() else None


class JsonlEventBus:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def publish(self, event: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
