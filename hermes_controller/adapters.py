"""Allow-listed worker execution adapters; no arbitrary command transport."""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
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
    """Controlled bridge to the existing cxh-run runner (never a shell bridge)."""

    def __init__(self, *, runner_path: str | os.PathLike[str], authorized_roots: list[str | os.PathLike[str]],
                 execution_profiles: set[str] | None = None, task_types: set[str] | None = None) -> None:
        self.runner_path = Path(runner_path).resolve()
        self.roots = [Path(item).resolve() for item in authorized_roots]
        self.profiles = execution_profiles or {"hermes"}
        self.task_types = task_types or {"development", "hermes_smoke"}

    def execute(self, task: dict[str, Any]) -> AdapterResult:
        try:
            return self._execute(task)
        except AdapterExecutionError as exc:
            return AdapterResult(status=exc.status, summary=str(exc), evidence=exc.evidence)

    def _execute(self, task: dict[str, Any]) -> AdapterResult:
        task_type = task.get("task_type")
        if task_type not in self.task_types: raise AdapterExecutionError("BLOCKED", "task_type is not allowed")
        profile = task.get("execution_profile")
        if profile not in self.profiles: raise AdapterExecutionError("BLOCKED", "unknown execution_profile")
        cwd = self._authorized(task.get("working_directory"), "working_directory")
        task_file = self._authorized(task.get("brain", {}).get("task_file"), "brain.task_file")
        output = self._authorized(task.get("run_output_dir"), "run_output_dir")
        for value in task.get("allowed_paths", []): self._authorized(value, "allowed_paths")
        if not self.runner_path.is_file(): raise AdapterExecutionError("BLOCKED", "cxh-run is not available")
        if not (cwd / ".git").exists(): raise AdapterExecutionError("BLOCKED", "working_directory is not a Git repository")
        timeout = task.get("timeout_seconds", 1800)
        if not isinstance(timeout, int) or not 1 <= timeout <= 7200: raise AdapterExecutionError("BLOCKED", "invalid timeout_seconds")
        output.mkdir(parents=True, exist_ok=True)
        log = output / "cxh-run.log"
        argv = [str(self.runner_path)]
        if task_type == "hermes_smoke":
            argv.append("--smoke-test")
        argv.extend(["--working-directory", str(cwd), "--task-file", str(task_file),
                     "--run-output-dir", str(output), "--execution-profile", profile, "--timeout-seconds", str(timeout)])
        try:
            with log.open("wb") as stream:
                process = subprocess.Popen(argv, cwd=cwd, stdout=stream, stderr=subprocess.STDOUT, shell=False)
                try: exit_code = process.wait(timeout=timeout + 10)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try: process.wait(timeout=10)
                    except subprocess.TimeoutExpired: process.kill(); process.wait()
                    raise AdapterExecutionError("FAILED", "cxh-run timed out", [str(log)])
        except OSError as exc:
            raise AdapterExecutionError("FAILED", f"could not start cxh-run: {exc}", [str(log)]) from exc
        result_path = output / "result.json"
        if not result_path.is_file(): raise AdapterExecutionError("FAILED", f"cxh-run exited {exit_code} without result.json", [str(log)])
        try: result = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc: raise AdapterExecutionError("FAILED", "invalid result.json", [str(log), str(result_path)]) from exc
        required = {"status", "summary", "gate", "completed", "remaining", "evidence"}
        if not isinstance(result, dict) or not required.issubset(result) or result["status"] not in {"DONE", "WAIT_USER", "BLOCKED", "FAILED"}:
            raise AdapterExecutionError("FAILED", "result.json violates Hermes result contract", [str(log), str(result_path)])
        if exit_code != 0 and result["status"] == "DONE":
            raise AdapterExecutionError("FAILED", f"cxh-run exited {exit_code}", [str(log), str(result_path)])
        evidence = [str(log), str(result_path), *[str(item) for item in result["evidence"]]]
        return AdapterResult(status=result["status"], summary=result["summary"], gate=result["gate"],
                             completed=result["completed"], remaining=result["remaining"], evidence=evidence)

    def _authorized(self, value: Any, name: str) -> Path:
        if not isinstance(value, str) or not value: raise AdapterExecutionError("BLOCKED", f"{name} is required")
        path = Path(value).resolve()
        if not any(path == root or root in path.parents for root in self.roots):
            raise AdapterExecutionError("BLOCKED", f"{name} is outside authorized roots")
        return path


class AdapterExecutionError(RuntimeError):
    def __init__(self, status: str, message: str, evidence: list[str] | None = None) -> None:
        super().__init__(message); self.status, self.evidence = status, evidence or []
