"""Versioned contracts and execution records shared by all adapters."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ValueType(StrEnum):
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    OBJECT = "object"
    ARRAY = "array"
    ANY = "any"


class ValueSpec(Contract):
    type: ValueType
    required: bool = True
    description: str = ""


def value_matches(value: Any, kind: ValueType) -> bool:
    return {
        ValueType.STRING: lambda: isinstance(value, str),
        ValueType.INTEGER: lambda: isinstance(value, int) and not isinstance(value, bool),
        ValueType.NUMBER: lambda: isinstance(value, (int, float)) and not isinstance(value, bool),
        ValueType.BOOLEAN: lambda: isinstance(value, bool),
        ValueType.OBJECT: lambda: isinstance(value, dict),
        ValueType.ARRAY: lambda: isinstance(value, list),
        ValueType.ANY: lambda: True,
    }[kind]()


def types_compatible(source: ValueType, target: ValueType) -> bool:
    return source == target or ValueType.ANY in (source, target) or (source == ValueType.INTEGER and target == ValueType.NUMBER)


class Permission(StrEnum):
    NETWORK = "network"
    FILESYSTEM_READ = "filesystem_read"
    FILESYSTEM_WRITE = "filesystem_write"
    BROWSER = "browser"
    SHELL = "shell"
    DATABASE = "database"
    EXTERNAL_API = "external_api"
    SECRETS = "secrets"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ErrorCode(StrEnum):
    VALIDATION_ERROR = "validation_error"
    INPUT_ERROR = "input_error"
    EXECUTOR_ERROR = "executor_error"
    TIMEOUT = "timeout"
    PERMISSION_ERROR = "permission_error"
    DEPENDENCY_ERROR = "dependency_error"
    EXTERNAL_SERVICE_ERROR = "external_service_error"
    OUTPUT_VALIDATION_ERROR = "output_validation_error"
    USER_REVIEW_REQUIRED = "user_review_required"
    UNKNOWN_ERROR = "unknown_error"


class ExecutionError(Contract):
    code: ErrorCode
    message: str
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class RetryPolicy(Contract):
    max_attempts: int = Field(default=1, ge=1, le=10)
    strategy: str = Field(default="fixed", pattern="^(fixed|exponential)$")
    delay_seconds: float = Field(default=0, ge=0, le=60)
    retry_on: list[ErrorCode] = Field(default_factory=list)


class ExecutorSpec(Contract):
    type: str = Field(min_length=1)
    entry: str = Field(min_length=1)
    args: list[str] = Field(default_factory=list)
    cwd: str | None = None
    language: str | None = None


class SkillDependency(Contract):
    skill_id: str
    version: str


class SkillDefinition(Contract):
    id: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    name: str = Field(min_length=1)
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    description: str = Field(min_length=1)
    category: str = "general"
    tags: list[str] = Field(default_factory=list)
    inputs: dict[str, ValueSpec] = Field(default_factory=dict)
    outputs: dict[str, ValueSpec] = Field(default_factory=dict)
    executor: ExecutorSpec
    permissions: list[Permission] = Field(default_factory=list)
    dependencies: list[SkillDependency] = Field(default_factory=list)
    secrets: list[str] = Field(default_factory=list)
    timeout_seconds: float = Field(default=30, gt=0, le=3600)
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    risk_level: RiskLevel = RiskLevel.LOW
    enabled: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class BindingType(StrEnum):
    LITERAL = "literal"
    WORKFLOW_INPUT = "workflow_input"
    NODE_OUTPUT = "node_output"
    CONTEXT = "context"


class InputBinding(Contract):
    type: BindingType
    value: Any = None
    name: str | None = None
    node_id: str | None = None
    field: str | None = None

    @model_validator(mode="after")
    def check_source(self) -> InputBinding:
        if self.type == BindingType.WORKFLOW_INPUT and not self.name:
            raise ValueError("workflow_input binding needs name")
        if self.type == BindingType.NODE_OUTPUT and (not self.node_id or not self.field):
            raise ValueError("node_output binding needs node_id and field")
        if self.type == BindingType.CONTEXT and not self.name:
            raise ValueError("context binding needs name")
        return self


class NodeType(StrEnum):
    SKILL = "skill"
    CONDITION = "condition"
    HUMAN_REVIEW = "human_review"
    MERGE = "merge"


class WorkflowNode(Contract):
    id: str
    type: NodeType = NodeType.SKILL
    skill_id: str | None = None
    skill_version: str | None = None
    inputs: dict[str, InputBinding] = Field(default_factory=dict)
    timeout_seconds: float | None = Field(default=None, gt=0)


class WorkflowEdge(Contract):
    source: str
    target: str
    condition: str | None = None


class WorkflowDefinition(Contract):
    id: str
    name: str
    version: int = Field(ge=1)
    inputs: dict[str, ValueSpec] = Field(default_factory=dict)
    outputs: dict[str, InputBinding] = Field(default_factory=dict)
    nodes: list[WorkflowNode] = Field(default_factory=list)
    edges: list[WorkflowEdge] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def version_id(self) -> str:
        return f"{self.id}:{self.version}"


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING = "waiting"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PARTIAL = "partial"


class NodeStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    WAITING_REVIEW = "waiting_review"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class Artifact(Contract):
    id: str
    uri: str
    media_type: str
    node_run_id: str
    size_bytes: int | None = None


class ReviewTask(Contract):
    id: str
    workflow_run_id: str
    node_run_id: str
    payload: dict[str, Any] = Field(default_factory=dict)
    reason: str
    choices: list[str] = Field(default_factory=list)
    status: str = "pending"
    reviewer: str | None = None
    decision: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    resolved_at: datetime | None = None


class SkillResult(Contract):
    status: NodeStatus
    outputs: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[Artifact] = Field(default_factory=list)
    logs: list[str] = Field(default_factory=list)
    metrics: dict[str, float] = Field(default_factory=dict)
    error: ExecutionError | None = None
    started_at: datetime = Field(default_factory=utc_now)
    finished_at: datetime | None = None


class NodeRun(Contract):
    id: str
    node_id: str
    skill_id: str
    skill_version: str
    skill_definition_snapshot: SkillDefinition
    status: NodeStatus = NodeStatus.PENDING
    attempt: int = 0
    inputs: dict[str, Any] = Field(default_factory=dict)
    result: SkillResult | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class WorkflowRun(Contract):
    id: str
    trace_id: str
    workflow_id: str
    workflow_version_id: str
    definition_snapshot: WorkflowDefinition
    status: RunStatus = RunStatus.PENDING
    inputs: dict[str, Any] = Field(default_factory=dict)
    outputs: dict[str, Any] = Field(default_factory=dict)
    node_runs: dict[str, NodeRun] = Field(default_factory=dict)
    review_tasks: list[ReviewTask] = Field(default_factory=list)
    error: ExecutionError | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
