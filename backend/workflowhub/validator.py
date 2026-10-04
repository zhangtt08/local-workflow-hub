"""Static Workflow validation happens before a Run can be created."""

from __future__ import annotations

from dataclasses import dataclass, field

from .dag import DAG
from .models import BindingType, Permission, ValueType, WorkflowDefinition, value_matches, types_compatible
from .ports import SkillRegistry


@dataclass
class ValidationReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.errors


class WorkflowValidator:
    def __init__(self, registry: SkillRegistry, executor_types: set[str], allowed_permissions: set[Permission]):
        self.registry = registry
        self.executor_types = executor_types
        self.allowed_permissions = allowed_permissions

    def validate(self, workflow: WorkflowDefinition) -> ValidationReport:
        report = ValidationReport()
        if not workflow.nodes:
            report.errors.append("Workflow must contain a node")
            return report
        try:
            dag = DAG(workflow)
        except ValueError as exc:
            report.errors.append(str(exc))
            return report
        nodes = {node.id: node for node in workflow.nodes}
        skills = {}
        for node in workflow.nodes:
            if node.type != "skill":
                report.errors.append(f"Node {node.id}: {node.type} execution is reserved for a later phase")
                continue
            if not node.skill_id or not node.skill_version:
                report.errors.append(f"Node {node.id}: skill_id and skill_version are required")
                continue
            skill = self.registry.get_version(node.skill_id, node.skill_version)
            if skill is None:
                report.errors.append(f"Node {node.id}: unknown Skill {node.skill_id}@{node.skill_version}")
                continue
            skills[node.id] = skill
            if not skill.enabled:
                report.errors.append(f"Node {node.id}: Skill is disabled")
            if skill.executor.type not in self.executor_types:
                report.errors.append(f"Node {node.id}: executor {skill.executor.type} is unavailable")
            denied = set(skill.permissions) - self.allowed_permissions
            if denied:
                report.errors.append(f"Node {node.id}: permissions denied: {', '.join(sorted(denied))}")
            for dependency in skill.dependencies:
                if self.registry.get_version(dependency.skill_id, dependency.version) is None:
                    report.errors.append(f"Node {node.id}: missing dependency {dependency.skill_id}@{dependency.version}")
            for name, spec in skill.inputs.items():
                binding = node.inputs.get(name)
                if binding is None:
                    if spec.required:
                        report.errors.append(f"Node {node.id}: missing required input {name}")
                    continue
                source_type: ValueType | None = None
                if binding.type == BindingType.LITERAL:
                    if not value_matches(binding.value, spec.type):
                        report.errors.append(f"Node {node.id}: literal input {name} has wrong type")
                    continue
                if binding.type == BindingType.WORKFLOW_INPUT:
                    source = workflow.inputs.get(binding.name or "")
                    if source is None:
                        report.errors.append(f"Node {node.id}: unknown Workflow input {binding.name}")
                    else:
                        source_type = source.type
                        if spec.required and not source.required:
                            report.errors.append(f"Node {node.id}: required input {name} depends on optional Workflow input {binding.name}")
                elif binding.type == BindingType.NODE_OUTPUT:
                    producer = nodes.get(binding.node_id or "")
                    if producer is None:
                        report.errors.append(f"Node {node.id}: unknown source node {binding.node_id}")
                    elif binding.node_id not in dag.parents[node.id]:
                        report.errors.append(f"Node {node.id}: source {binding.node_id} needs a direct dependency edge")
                    producer_skill = self.registry.get_version(producer.skill_id, producer.skill_version) if producer and producer.skill_id and producer.skill_version else None
                    output = producer_skill.outputs.get(binding.field or "") if producer_skill else None
                    if output is None:
                        report.errors.append(f"Node {node.id}: unknown output {binding.node_id}.{binding.field}")
                    else:
                        source_type = output.type
                elif binding.type == BindingType.CONTEXT:
                    if binding.name not in {"trace_id", "workflow_run_id", "node_run_id"}:
                        report.errors.append(f"Node {node.id}: unsupported context key {binding.name}")
                    source_type = ValueType.STRING
                if source_type and not types_compatible(source_type, spec.type):
                    report.errors.append(f"Node {node.id}: {name} expects {spec.type}, received {source_type}")
            for name in node.inputs.keys() - skill.inputs.keys():
                report.errors.append(f"Node {node.id}: undeclared input {name}")
            if len(workflow.nodes) > 1 and not dag.parents[node.id] and not dag.children[node.id]:
                report.warnings.append(f"Node {node.id} is isolated")
        for name, binding in workflow.outputs.items():
            if binding.type == BindingType.NODE_OUTPUT:
                skill = skills.get(binding.node_id or "")
                if skill is None or binding.field not in skill.outputs:
                    report.errors.append(f"Workflow output {name} references unknown node output")
            elif binding.type == BindingType.WORKFLOW_INPUT and binding.name not in workflow.inputs:
                report.errors.append(f"Workflow output {name} references unknown Workflow input")
            elif binding.type == BindingType.CONTEXT and binding.name not in {"trace_id", "workflow_run_id"}:
                report.errors.append(f"Workflow output {name} references unsupported context key")
        if any(edge.condition is not None for edge in workflow.edges):
            report.errors.append("Conditional edges are reserved for a later phase")
        return report
