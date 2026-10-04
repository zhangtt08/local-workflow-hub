"""Sequential deterministic MVP scheduler over a DAG; executor adapters stay outside."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from uuid import uuid4

from .bindings import resolve_binding
from .dag import DAG
from .errors import WorkflowHubError
from .executors import ExecutorRouter
from .models import ErrorCode, ExecutionError, NodeRun, NodeStatus, Permission, ReviewTask, RunStatus, SkillDefinition, SkillResult, WorkflowDefinition, WorkflowRun, utc_now, value_matches
from .ports import EventBus, ExecutionContext, RunRepository, SkillRegistry
from .state import transition_node, transition_run
from .validator import WorkflowValidator


class WorkflowRunner:
    def __init__(
        self,
        registry: SkillRegistry,
        executors: ExecutorRouter,
        runs: RunRepository,
        events: EventBus,
        artifact_root: Path,
        allowed_permissions: set[Permission],
        system_max_timeout: float = 300,
    ):
        self.registry = registry
        self.executors = executors
        self.runs = runs
        self.events = events
        self.artifact_root = artifact_root
        self.allowed_permissions = allowed_permissions
        self.system_max_timeout = system_max_timeout
        self.validator = WorkflowValidator(registry, executors.types, allowed_permissions)

    def _emit(self, run: WorkflowRun, event: str, node_run: NodeRun | None = None) -> None:
        self.events.publish({
            "event": event,
            "at": utc_now().isoformat(),
            "trace_id": run.trace_id,
            "workflow_run_id": run.id,
            "node_run_id": node_run.id if node_run else None,
            "node_id": node_run.node_id if node_run else None,
            "skill_id": node_run.skill_id if node_run else None,
            "attempt": node_run.attempt if node_run else None,
            "run_status": run.status,
            "node_status": node_run.status if node_run else None,
            "error_code": node_run.result.error.code if node_run and node_run.result and node_run.result.error else None,
        })

    async def run(self, workflow: WorkflowDefinition, inputs: dict[str, Any]) -> WorkflowRun:
        report = self.validator.validate(workflow)
        if not report.valid:
            raise WorkflowHubError(ErrorCode.VALIDATION_ERROR, "; ".join(report.errors))
        for name, spec in workflow.inputs.items():
            if name not in inputs:
                if spec.required:
                    raise WorkflowHubError(ErrorCode.INPUT_ERROR, f"Missing Workflow input: {name}")
            elif not value_matches(inputs[name], spec.type):
                raise WorkflowHubError(ErrorCode.INPUT_ERROR, f"Invalid Workflow input type: {name}")
        unknown = inputs.keys() - workflow.inputs.keys()
        if unknown:
            raise WorkflowHubError(ErrorCode.INPUT_ERROR, f"Unknown Workflow inputs: {', '.join(sorted(unknown))}")
        snapshot = WorkflowDefinition.model_validate(workflow.model_dump())
        run = WorkflowRun(
            id=uuid4().hex,
            trace_id=uuid4().hex,
            workflow_id=snapshot.id,
            workflow_version_id=snapshot.version_id,
            definition_snapshot=snapshot,
            inputs=dict(inputs),
            started_at=utc_now(),
        )
        transition_run(run, RunStatus.RUNNING)
        node_map = {node.id: node for node in snapshot.nodes}
        order = DAG(snapshot).topological_order()
        for node_id in order:
            node = node_map[node_id]
            skill = self.registry.get_version(node.skill_id, node.skill_version)  # checked by validator
            assert skill is not None
            run.node_runs[node_id] = NodeRun(
                id=uuid4().hex, node_id=node_id, skill_id=skill.id, skill_version=skill.version,
                skill_definition_snapshot=SkillDefinition.model_validate(skill.model_dump()),
            )
        self.runs.save(run)
        self._emit(run, "workflow.started")
        outputs_by_node: dict[str, dict[str, Any]] = {}
        context_values = {"trace_id": run.trace_id, "workflow_run_id": run.id}
        for node_id in order:
            node = node_map[node_id]
            node_run = run.node_runs[node_id]
            skill = node_run.skill_definition_snapshot
            assert skill is not None
            transition_node(node_run, NodeStatus.READY)
            self._emit(run, "node.ready", node_run)
            try:
                binding_context = {**context_values, "node_run_id": node_run.id}
                node_inputs = {name: resolve_binding(binding, inputs, outputs_by_node, binding_context) for name, binding in node.inputs.items()}
                for name, spec in skill.inputs.items():
                    if name in node_inputs and not value_matches(node_inputs[name], spec.type):
                        raise WorkflowHubError(ErrorCode.INPUT_ERROR, f"Node {node_id} input {name} failed runtime validation")
                node_run.inputs = node_inputs
                result = await self._execute_with_retry(run, node_run, skill, node_inputs, node.timeout_seconds)
            except WorkflowHubError as exc:
                result = SkillResult(status=NodeStatus.FAILED, error=exc.detail, finished_at=utc_now())
            except (KeyError, TypeError, ValueError) as exc:
                result = SkillResult(status=NodeStatus.FAILED, error=ExecutionError(code=ErrorCode.INPUT_ERROR, message=str(exc)), finished_at=utc_now())
            node_run.result = result
            transition_node(node_run, result.status)
            node_run.finished_at = utc_now()
            self.runs.save(run)
            if result.status == NodeStatus.WAITING_REVIEW:
                transition_run(run, RunStatus.WAITING)
                run.review_tasks.append(ReviewTask(
                    id=uuid4().hex, workflow_run_id=run.id, node_run_id=node_run.id,
                    payload=result.outputs,
                    reason=result.error.message if result.error else f"Node {node_id} requests review",
                ))
                self.runs.save(run)
                self._emit(run, "review.requested", node_run)
                return run
            if result.status != NodeStatus.SUCCEEDED:
                run.error = result.error or ExecutionError(code=ErrorCode.UNKNOWN_ERROR, message=f"Node {node_id} failed")
                transition_run(run, RunStatus.FAILED)
                self._emit(run, "node.failed", node_run)
                break
            outputs_by_node[node_id] = result.outputs
            self._emit(run, "node.succeeded", node_run)
        if run.status == RunStatus.RUNNING:
            try:
                run.outputs = {name: resolve_binding(binding, inputs, outputs_by_node, context_values) for name, binding in snapshot.outputs.items()}
                transition_run(run, RunStatus.SUCCEEDED)
            except (KeyError, TypeError, ValueError) as exc:
                run.error = ExecutionError(code=ErrorCode.OUTPUT_VALIDATION_ERROR, message=str(exc))
                transition_run(run, RunStatus.FAILED)
        else:
            for node_run in run.node_runs.values():
                if node_run.status == NodeStatus.PENDING:
                    transition_node(node_run, NodeStatus.SKIPPED)
        run.finished_at = utc_now()
        self.runs.save(run)
        self._emit(run, "workflow.completed")
        return run

    async def _execute_with_retry(self, run: WorkflowRun, node_run: NodeRun, skill: SkillDefinition, inputs: dict[str, Any], override_timeout: float | None) -> SkillResult:
        timeout = min(override_timeout or skill.timeout_seconds, self.system_max_timeout)
        if timeout <= 0:
            raise WorkflowHubError(ErrorCode.VALIDATION_ERROR, "System timeout must be positive")
        policy = skill.retry_policy
        context = ExecutionContext(
            trace_id=run.trace_id, workflow_run_id=run.id, node_run_id=node_run.id,
            artifact_root=self.artifact_root / run.id,
            allowed_permissions=frozenset(self.allowed_permissions),
        )
        for attempt in range(1, policy.max_attempts + 1):
            node_run.attempt = attempt
            transition_node(node_run, NodeStatus.RUNNING)
            node_run.started_at = utc_now()
            self.runs.save(run)
            self._emit(run, "node.started", node_run)
            try:
                result = await self.executors.get(skill.executor.type).execute(skill, inputs, context, timeout)
            except WorkflowHubError as exc:
                result = SkillResult(status=NodeStatus.FAILED, error=exc.detail, finished_at=utc_now())
            except Exception as exc:
                result = SkillResult(status=NodeStatus.FAILED, error=ExecutionError(code=ErrorCode.EXECUTOR_ERROR, message=str(exc)), finished_at=utc_now())
            if result.status == NodeStatus.SUCCEEDED:
                error = self._validate_outputs(skill, result.outputs)
                if error:
                    result = SkillResult(status=NodeStatus.FAILED, outputs=result.outputs, artifacts=result.artifacts, logs=result.logs, error=error, started_at=result.started_at, finished_at=utc_now())
            if result.status == NodeStatus.SUCCEEDED:
                return result
            if attempt >= policy.max_attempts or not result.error or result.error.code not in policy.retry_on:
                return result
            self._emit(run, "node.retry", node_run)
            delay = policy.delay_seconds * (2 ** (attempt - 1) if policy.strategy == "exponential" else 1)
            if delay:
                await asyncio.sleep(delay)
        raise AssertionError("Retry loop should return")

    @staticmethod
    def _validate_outputs(skill: SkillDefinition, outputs: dict[str, Any]) -> ExecutionError | None:
        for name, spec in skill.outputs.items():
            if name not in outputs:
                if spec.required:
                    return ExecutionError(code=ErrorCode.OUTPUT_VALIDATION_ERROR, message=f"Missing Skill output: {name}")
            elif outputs[name] is None and not spec.required:
                continue
            elif not value_matches(outputs[name], spec.type):
                return ExecutionError(code=ErrorCode.OUTPUT_VALIDATION_ERROR, message=f"Invalid Skill output type: {name}")
        unknown = outputs.keys() - skill.outputs.keys()
        if unknown:
            return ExecutionError(code=ErrorCode.OUTPUT_VALIDATION_ERROR, message=f"Undeclared Skill outputs: {', '.join(sorted(unknown))}")
        return None
