"""Structured errors at the boundary between contracts and execution."""

from typing import Any

from .models import ErrorCode, ExecutionError


class WorkflowHubError(Exception):
    def __init__(self, code: ErrorCode, message: str, details: dict[str, Any] | None = None,
                 retryable: bool = False):
        super().__init__(message)
        self.detail = ExecutionError(code=code, message=message, retryable=retryable,
                                     details=details or {})
