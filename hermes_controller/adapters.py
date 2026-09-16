"""Allow-listed worker execution adapters; no arbitrary command transport."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class AdapterResult:
    status: str = "DONE"
    summary: str = "mock execution completed"
    gate: str | None = None
    completed: list[str] | None = None
    remaining: list[str] | None = None
    evidence: list[dict[str, Any]] | None = None

    def result(self) -> dict[str, Any]:
        return {"status": self.status, "summary": self.summary, "gate": self.gate,
                "completed": self.completed or [], "remaining": self.remaining or [], "evidence": self.evidence or []}


class ExecutionAdapter(Protocol):
    def execute(self, task: dict[str, Any]) -> AdapterResult: ...


class MockAdapter:
    """Deterministic adapter configured through validated test-only task arguments."""
    def execute(self, task: dict[str, Any]) -> AdapterResult:
        options = task.get("mock", {})
        if not isinstance(options, dict):
            raise ValueError("mock options must be an object")
        delay_ms = options.get("delay_ms", 0)
        if not isinstance(delay_ms, int) or not 0 <= delay_ms <= 60_000:
            raise ValueError("invalid mock delay")
        if delay_ms: time.sleep(delay_ms / 1000)
        status = options.get("status", "DONE")
        if status not in {"DONE", "WAIT_USER", "BLOCKED", "FAILED"}: raise ValueError("invalid mock status")
        return AdapterResult(status=status, summary=options.get("summary", "mock execution completed"), gate=options.get("gate"))


class CxhRunAdapter:
    """Phase 2B contract only. This class intentionally never starts a process."""
    def execute(self, task: dict[str, Any]) -> AdapterResult:
        raise RuntimeError("CxhRunAdapter is reserved for Phase 2B and cannot execute in Phase 2A")

    # Phase 2B will invoke cxh-run/cxh-run.bat with an explicit cwd, a validated
    # execution_profile and authorized paths; it will consume result.json, enforce
    # timeout/cancellation, and attach bounded logs. It must not duplicate runner logic.
