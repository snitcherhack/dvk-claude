"""Allow-listed worker execution adapters; no arbitrary command transport."""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol


@dataclass(frozen=True)
class AdapterResult:
    status: str = "DONE"
    summary: str = "mock execution completed"
    gate: str | None = None
    completed: list[str] | None = None
    remaining: list[str] | None = None
    evidence: list[str] | None = None

    def result(self) -> dict[str, Any]:
        return {"status": self.status, "summary": self.summary, "gate": self.gate,
                "completed": self.completed or [], "remaining": self.remaining or [], "evidence": self.evidence or []}


class ExecutionAdapter(Protocol):
    def execute(self, task: dict[str, Any]) -> AdapterResult: ...


HERMES_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["status", "summary", "gate", "completed", "remaining", "evidence"],
    "properties": {
        "status": {"type": "string", "enum": ["DONE", "WAIT_USER", "BLOCKED", "FAILED"]},
        "summary": {"type": "string"},
        "gate": {"type": ["string", "null"]},
        "completed": {"type": "array", "items": {"type": "string"}},
        "remaining": {"type": "array", "items": {"type": "string"}},
        "evidence": {"type": "array", "items": {"type": "string"}},
    },
}

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


class EngineRoutingAdapter:
    """Route a validated task to a configured execution engine."""

    def __init__(self, adapters: dict[str, ExecutionAdapter], *, default_engine: str = "codex") -> None:
        if not adapters:
            raise ValueError("at least one execution engine is required")
        if default_engine not in adapters:
            raise ValueError("default execution engine is not configured")
        self.adapters = dict(adapters)
        self.default_engine = default_engine

    def execute(self, task: dict[str, Any]) -> AdapterResult:
        engine = task.get("execution_engine")
        if engine is None:
            capabilities = task.get("required_capabilities")
            if isinstance(capabilities, list):
                engine = "codex" if "codex" in capabilities else "native"
            else:
                engine = self.default_engine
        if not isinstance(engine, str) or not engine:
            return AdapterResult(status="BLOCKED", summary="invalid execution_engine")
        adapter = self.adapters.get(engine)
        if adapter is None:
            return AdapterResult(status="BLOCKED", summary=f"execution engine is not configured: {engine}")
        return adapter.execute(task)


class ClaudeAgentAdapter:
    """Fail-closed bridge to Claude Agent SDK with deterministic tool boundaries."""

    MIN_SDK_VERSION = (0, 2, 140)
    READ_ONLY_TOOLS = ("Read", "Glob", "Grep")
    WRITE_TOOLS = ("Write", "Edit")

    def __init__(
        self, *, authorized_roots: list[str | os.PathLike[str]],
        execution_profiles: set[str] | None = None, task_types: set[str] | None = None,
        max_turns: int = 6, subscription_only: bool = True,
        cli_path: str | os.PathLike[str] | None = None,
        runner: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        self.roots = [Path(item).resolve() for item in authorized_roots]
        if not self.roots:
            raise ValueError("at least one authorized root is required")
        self.profiles = execution_profiles or {"claude_smoke", "hermes"}
        self.task_types = task_types or {"claude_smoke", "development"}
        if not isinstance(max_turns, int) or not 1 <= max_turns <= 50:
            raise ValueError("invalid max_turns")
        self.max_turns = max_turns
        self.subscription_only = subscription_only
        self.cli_path = Path(cli_path).resolve() if cli_path else None
        self.runner = runner

    def execute(self, task: dict[str, Any]) -> AdapterResult:
        try:
            return self._execute(task)
        except AdapterExecutionError as exc:
            return AdapterResult(status=exc.status, summary=str(exc), evidence=exc.evidence)
        except TimeoutError:
            return AdapterResult(status="FAILED", summary="Claude agent timed out")
        except Exception as exc:
            return AdapterResult(status="FAILED", summary=f"Claude agent execution failed: {type(exc).__name__}")

    def _execute(self, task: dict[str, Any]) -> AdapterResult:
        task_type = task.get("task_type")
        if task_type not in self.task_types:
            raise AdapterExecutionError("BLOCKED", "task_type is not allowed for Claude")
        profile = task.get("execution_profile")
        if profile not in self.profiles:
            raise AdapterExecutionError("BLOCKED", "unknown Claude execution_profile")
        cwd = self._authorized(task.get("working_directory"), "working_directory")
        task_file = self._authorized(task.get("brain", {}).get("task_file"), "brain.task_file")
        output = self._authorized(task.get("run_output_dir"), "run_output_dir")
        allowed_paths = task.get("allowed_paths", [])
        if not isinstance(allowed_paths, list):
            raise AdapterExecutionError("BLOCKED", "allowed_paths must be a list")
        tool_roots = [self._authorized(value, "allowed_paths") for value in allowed_paths]
        if not tool_roots:
            tool_roots = [cwd, task_file.parent.resolve()]
        tool_roots = list(dict.fromkeys(tool_roots))
        if not (cwd / ".git").exists():
            raise AdapterExecutionError("BLOCKED", "working_directory is not a Git repository")
        timeout = task.get("timeout_seconds", 1800)
        if not isinstance(timeout, int) or not 1 <= timeout <= 7200:
            raise AdapterExecutionError("BLOCKED", "invalid timeout_seconds")
        max_turns = task.get("max_turns", self.max_turns)
        if not isinstance(max_turns, int) or not 1 <= max_turns <= 50:
            raise AdapterExecutionError("BLOCKED", "invalid max_turns")
        try:
            if task_file.stat().st_size > 1_048_576:
                raise AdapterExecutionError("BLOCKED", "brain.task_file exceeds 1 MiB")
            task_text = task_file.read_text(encoding="utf-8")
        except OSError as exc:
            raise AdapterExecutionError("BLOCKED", f"could not read brain.task_file: {exc}") from exc

        tools = list(self.READ_ONLY_TOOLS)
        if profile == "hermes":
            tools.extend(self.WRITE_TOOLS)
        request = {
            "cwd": str(cwd),
            "authorized_roots": [str(root) for root in tool_roots],
            "task_file": str(task_file),
            "task_text": task_text,
            "profile": profile,
            "allowed_tools": tools,
            "timeout_seconds": timeout,
            "max_turns": max_turns,
            "result_schema": HERMES_RESULT_SCHEMA,
        }
        output.mkdir(parents=True, exist_ok=True)
        log = output / "claude-agent.log"
        runner = self.runner or self._run_sdk
        result = runner(request)
        self._validate_result(result)
        log.write_text(json.dumps({
            "profile": profile, "allowed_tools": tools, "result": result
        }, sort_keys=True, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        evidence = [str(log), *[str(item) for item in result["evidence"]]]
        return AdapterResult(
            status=result["status"], summary=result["summary"], gate=result["gate"],
            completed=result["completed"], remaining=result["remaining"], evidence=evidence,
        )

    def _run_sdk(self, request: dict[str, Any]) -> dict[str, Any]:
        if self.subscription_only:
            external_provider_vars = (
                "ANTHROPIC_API_KEY",
                "CLAUDE_CODE_USE_BEDROCK",
                "CLAUDE_CODE_USE_VERTEX",
                "CLAUDE_CODE_USE_FOUNDRY",
            )
            configured = [name for name in external_provider_vars if os.environ.get(name)]
            if configured:
                raise AdapterExecutionError(
                    "BLOCKED",
                    "subscription-only policy rejects external provider configuration: " + ", ".join(configured),
                )
            if self.cli_path is None:
                raise AdapterExecutionError("BLOCKED", "subscription-only policy requires an authenticated Claude CLI path")
        if self.cli_path is not None and not self.cli_path.is_file():
            raise AdapterExecutionError("BLOCKED", "configured Claude CLI path is not available")
        try:
            import anyio
            from importlib.metadata import PackageNotFoundError, version
            from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKError, ResultMessage, query
            from claude_agent_sdk.types import HookMatcher
        except ImportError as exc:
            raise AdapterExecutionError("BLOCKED", "claude-agent-sdk is not available") from exc
        try:
            installed = tuple(int(part) for part in version("claude-agent-sdk").split(".")[:3])
        except (PackageNotFoundError, ValueError, TypeError) as exc:
            raise AdapterExecutionError("BLOCKED", "could not determine claude-agent-sdk version") from exc
        if installed < self.MIN_SDK_VERSION:
            minimum = ".".join(str(part) for part in self.MIN_SDK_VERSION)
            raise AdapterExecutionError("BLOCKED", f"claude-agent-sdk >= {minimum} is required")

        cwd = Path(request["cwd"]).resolve()
        tool_roots = [Path(item).resolve() for item in request["authorized_roots"]]

        async def guard_path(input_data: Any, _tool_use_id: str | None, _context: Any) -> dict[str, Any]:
            tool_name = input_data.get("tool_name")
            tool_input = input_data.get("tool_input", {})
            if self._tool_input_authorized(tool_name, tool_input, cwd, tool_roots):
                return {}
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": "Hermes path policy denied access outside authorized roots",
                }
            }

        async def invoke() -> dict[str, Any]:
            profile = request["profile"]
            mode_note = "Read-only smoke validation. Do not modify files." if profile == "claude_smoke" else "Edits are allowed only through Write/Edit inside authorized roots. Bash and network tools are unavailable."
            prompt = (
                f"{mode_note}\n"
                "Follow the task snapshot below. Do not use tools outside the allow-list. "
                "Return the final result through the required structured-output schema.\n\n"
                f"TASK FILE: {request['task_file']}\n\n{request['task_text']}"
            )
            options = ClaudeAgentOptions(
                cli_path=str(self.cli_path) if self.cli_path is not None else None,
                cwd=request["cwd"],
                add_dirs=request["authorized_roots"],
                allowed_tools=request["allowed_tools"],
                disallowed_tools=["Bash", "WebFetch", "WebSearch", "NotebookEdit"],
                permission_mode="dontAsk",
                max_turns=request["max_turns"],
                output_format={"type": "json_schema", "schema": request["result_schema"]},
                setting_sources=[],
                skills=[],
                system_prompt="You are a Hermes execution worker. Follow the explicit tool and path policy exactly.",
                hooks={"PreToolUse": [HookMatcher(matcher="Read|Glob|Grep|Write|Edit", hooks=[guard_path])]},
            )
            structured: dict[str, Any] | None = None
            error_summary: str | None = None
            try:
                with anyio.fail_after(request["timeout_seconds"]):
                    async for message in query(prompt=prompt, options=options):
                        if isinstance(message, ResultMessage):
                            if getattr(message, "is_error", False):
                                error_summary = getattr(message, "result", None) or getattr(message, "subtype", "unknown")
                                continue
                            candidate = getattr(message, "structured_output", None)
                            if isinstance(candidate, dict):
                                structured = candidate
            except ClaudeSDKError as exc:
                if error_summary:
                    raise AdapterExecutionError("FAILED", f"Claude returned an error result: {error_summary}") from exc
                raise AdapterExecutionError("FAILED", f"Claude SDK error: {type(exc).__name__}") from exc
            if error_summary:
                raise AdapterExecutionError("FAILED", f"Claude returned an error result: {error_summary}")
            if structured is None:
                raise AdapterExecutionError("FAILED", "Claude returned no structured result")
            return structured

        return anyio.run(invoke)

    def _tool_input_authorized(self, tool_name: Any, tool_input: Any, cwd: Path, roots: list[Path] | None = None) -> bool:
        if tool_name not in {*self.READ_ONLY_TOOLS, *self.WRITE_TOOLS} or not isinstance(tool_input, dict):
            return False
        key = "file_path" if tool_name in {"Read", "Write", "Edit"} else "path"
        raw = tool_input.get(key)
        if raw is None or raw == "":
            path = cwd
        elif not isinstance(raw, str):
            return False
        else:
            path = Path(raw)
            if not path.is_absolute():
                path = cwd / path
            path = path.resolve()
        if tool_name == "Glob":
            pattern = tool_input.get("pattern", "")
            if not isinstance(pattern, str):
                return False
            pattern_path = Path(pattern)
            if ".." in pattern_path.parts:
                return False
            if pattern_path.is_absolute() and not self._path_authorized(pattern_path.resolve(), roots):
                return False
        return self._path_authorized(path, roots)

    def _path_authorized(self, path: Path, roots: list[Path] | None = None) -> bool:
        active_roots = roots if roots is not None else self.roots
        return any(path == root or root in path.parents for root in active_roots)

    def _authorized(self, value: Any, name: str) -> Path:
        if not isinstance(value, str) or not value:
            raise AdapterExecutionError("BLOCKED", f"{name} is required")
        path = Path(value).resolve()
        if not self._path_authorized(path):
            raise AdapterExecutionError("BLOCKED", f"{name} is outside authorized roots")
        return path

    @staticmethod
    def _validate_result(result: Any) -> None:
        required = {"status", "summary", "gate", "completed", "remaining", "evidence"}
        if not isinstance(result, dict) or set(result) != required:
            raise AdapterExecutionError("FAILED", "Claude result violates Hermes result contract")
        if result["status"] not in {"DONE", "WAIT_USER", "BLOCKED", "FAILED"}:
            raise AdapterExecutionError("FAILED", "Claude result has invalid status")
        if not isinstance(result["summary"], str) or result["gate"] is not None and not isinstance(result["gate"], str):
            raise AdapterExecutionError("FAILED", "Claude result has invalid scalar fields")
        if any(not isinstance(result[name], list) or not all(isinstance(item, str) for item in result[name]) for name in ("completed", "remaining", "evidence")):
            raise AdapterExecutionError("FAILED", "Claude result has invalid list fields")


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
