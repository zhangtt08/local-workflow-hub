"""Deterministic graph operations, independent of Skill implementations."""

from __future__ import annotations

import heapq

from .models import WorkflowDefinition


class DAG:
    def __init__(self, workflow: WorkflowDefinition):
        ids = [node.id for node in workflow.nodes]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate node id")
        self.nodes = set(ids)
        self.parents: dict[str, set[str]] = {node_id: set() for node_id in ids}
        self.children: dict[str, set[str]] = {node_id: set() for node_id in ids}
        for edge in workflow.edges:
            if edge.source not in self.nodes or edge.target not in self.nodes:
                raise ValueError(f"Edge references unknown node: {edge.source} -> {edge.target}")
            if edge.target in self.children[edge.source]:
                raise ValueError(f"Duplicate edge: {edge.source} -> {edge.target}")
            self.children[edge.source].add(edge.target)
            self.parents[edge.target].add(edge.source)
        self.topological_order()  # fail before execution

    def topological_order(self) -> list[str]:
        degree = {node: len(parents) for node, parents in self.parents.items()}
        ready = [node for node, count in degree.items() if count == 0]
        heapq.heapify(ready)
        order = []
        while ready:
            node = heapq.heappop(ready)
            order.append(node)
            for child in sorted(self.children[node]):
                degree[child] -= 1
                if degree[child] == 0:
                    heapq.heappush(ready, child)
        if len(order) != len(self.nodes):
            raise ValueError("Workflow contains a cycle")
        return order

    def ready_nodes(self, completed: set[str], active: set[str] | None = None) -> list[str]:
        active = active or set()
        return [node for node in self.topological_order() if node not in completed | active and self.parents[node] <= completed]
