"""Explicit Run/NodeRun state transitions; terminal states cannot be reopened."""

from .models import NodeRun, NodeStatus, RunStatus, WorkflowRun


RUN_TRANSITIONS = {
    RunStatus.PENDING: {RunStatus.RUNNING, RunStatus.CANCELLED},
    RunStatus.RUNNING: {RunStatus.WAITING, RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.PARTIAL},
    RunStatus.WAITING: {RunStatus.RUNNING, RunStatus.CANCELLED, RunStatus.FAILED},
    RunStatus.SUCCEEDED: set(),
    RunStatus.FAILED: set(),
    RunStatus.CANCELLED: set(),
    RunStatus.PARTIAL: set(),
}

NODE_TRANSITIONS = {
    NodeStatus.PENDING: {NodeStatus.READY, NodeStatus.SKIPPED, NodeStatus.CANCELLED},
    NodeStatus.READY: {NodeStatus.RUNNING, NodeStatus.FAILED, NodeStatus.CANCELLED},
    NodeStatus.RUNNING: {NodeStatus.SUCCEEDED, NodeStatus.FAILED, NodeStatus.WAITING_REVIEW, NodeStatus.CANCELLED},
    NodeStatus.WAITING_REVIEW: {NodeStatus.RUNNING, NodeStatus.SUCCEEDED, NodeStatus.CANCELLED},
    NodeStatus.SUCCEEDED: set(),
    NodeStatus.FAILED: set(),
    NodeStatus.SKIPPED: set(),
    NodeStatus.CANCELLED: set(),
}


def transition_run(run: WorkflowRun, target: RunStatus) -> None:
    if target != run.status:
        if target not in RUN_TRANSITIONS[run.status]:
            raise ValueError(f"Invalid Run transition: {run.status} -> {target}")
        run.status = target


def transition_node(node_run: NodeRun, target: NodeStatus) -> None:
    if target != node_run.status:
        if target not in NODE_TRANSITIONS[node_run.status]:
            raise ValueError(f"Invalid Node transition: {node_run.status} -> {target}")
        node_run.status = target
