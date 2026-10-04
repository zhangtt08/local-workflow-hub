"""Ports owned by the domain/application layer; adapters implement these protocols."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .models import Artifact, Permission, SkillDefinition, SkillResult, WorkflowDefinition, WorkflowRun


@dataclass(frozen=True)
class ExecutionContext:
    trace_id: str
    workflow_run_id: str
    node_run_id: str
    artifact_root: Path
    allowed_permissions: frozenset[Permission]


class SkillExecutor(Protocol):
    async def execute(self, skill: SkillDefinition, inputs: dict[str, Any], context: ExecutionContext, timeout: float) -> SkillResult: ...


class SkillRegistry(Protocol):
    def refresh(self) -> int: ...
    def get_version(self, skill_id: str, version: str) -> SkillDefinition | None: ...
    def list(self) -> list[SkillDefinition]: ...


class SkillRepository(Protocol):
    def save(self, skill: SkillDefinition) -> None: ...
    def get_version(self, skill_id: str, version: str) -> SkillDefinition | None: ...


class WorkflowRepository(Protocol):
    def save_version(self, workflow: WorkflowDefinition) -> None: ...
    def get_version(self, workflow_id: str, version: int) -> WorkflowDefinition | None: ...


class RunRepository(Protocol):
    def save(self, run: WorkflowRun) -> None: ...
    def get(self, run_id: str) -> WorkflowRun | None: ...


class SkillSearchProvider(Protocol):
    def search(self, query: str, top_k: int = 10, category: str | None = None) -> list[SkillDefinition]: ...


class LLMProvider(Protocol):
    async def generate_workflow_draft(self, goal: str, skills: list[SkillDefinition]) -> WorkflowDefinition: ...


class ArtifactStorage(Protocol):
    def register(self, path: Path, node_run_id: str, media_type: str) -> Artifact: ...


class SecretProvider(Protocol):
    def get_secret(self, reference: str) -> str: ...


class EventBus(Protocol):
    def publish(self, event: dict[str, Any]) -> None: ...
