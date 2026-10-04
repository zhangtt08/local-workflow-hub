"""Explicit and inspectable input/output transfer between nodes."""

from __future__ import annotations

from typing import Any

from .models import BindingType, InputBinding


def resolve_binding(binding: InputBinding, workflow_inputs: dict[str, Any], node_outputs: dict[str, dict[str, Any]], context: dict[str, Any]) -> Any:
    if binding.type == BindingType.LITERAL:
        return binding.value
    if binding.type == BindingType.WORKFLOW_INPUT:
        return workflow_inputs[binding.name]  # type: ignore[index]
    if binding.type == BindingType.NODE_OUTPUT:
        return node_outputs[binding.node_id][binding.field]  # type: ignore[index]
    if binding.type == BindingType.CONTEXT:
        return context[binding.name]  # type: ignore[index]
    raise ValueError(f"Unsupported binding: {binding.type}")
