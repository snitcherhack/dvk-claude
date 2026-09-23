from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_controller.adapters import (
    AdapterExecutionError,
    ClaudeAgentAdapter,
    StructuredExecutionError,
    StructuredExecutionResult,
)
from hermes_controller.brainstorm_core import STAGE_SCHEMAS

PROPOSALS = {"proposals": [{"title": "A", "concept": "B"}]}


def layout(root: Path) -> dict[str, Path]:
    repo = root / "repo"
    (repo / ".git").mkdir(parents=True)
    job = root / "runtime" / "job-1"
    claude_stage = job / "stages" / "claude-proposals"
    codex_stage = job / "stages" / "codex-proposals"
    orchestration = job / "orchestration"
    for path in (claude_stage, codex_stage, orchestration):
        path.mkdir(parents=True)
    (claude_stage / "task.md").write_text("Propose formats.", encoding="utf-8")
    (codex_stage / "proposals.json").write_text("{}", encoding="utf-8")
    (orchestration / "baseline.json").write_text("{}", encoding="utf-8")
    return {"repo": repo, "job": job, "stage": claude_stage, "sibling": codex_stage, "orchestration": orchestration}


def brainstorm_task(paths: dict[str, Path], **overrides) -> dict:
    spec = {
        "task_type": "brainstorm",
        "execution_profile": "brainstorm",
        "working_directory": str(paths["repo"]),
        "brain": {"task_file": str(paths["stage"] / "task.md")},
        "run_output_dir": str(paths["stage"]),
        "timeout_seconds": 1800,
        "max_turns": 8,
        "allowed_paths": [str(paths["repo"]), str(paths["job"])],
    }
    spec.update(overrides)
    return spec


def adapter(root: Path, runner=None, **kwargs) -> ClaudeAgentAdapter:
    return ClaudeAgentAdapter(
        authorized_roots=[root],
        execution_profiles={"claude_smoke", "hermes", "brainstorm"},
        task_types={"claude_smoke", "development", "brainstorm"},
        runner=runner, **kwargs,
    )


def run_stage(root: Path, *, payload=PROPOSALS, stage="proposals", roots=None, timeout=60, task_overrides=None, **kwargs):
    paths = layout(root)
    seen: list[dict] = []

    def runner(request):
        seen.append(request)
        return payload

    instance = adapter(root, runner, **kwargs)
    result = instance.execute_structured(
        brainstorm_task(paths, **(task_overrides or {})),
        stage_schema=stage,
        stage_roots=roots if roots is not None else [str(paths["repo"]), str(paths["stage"])],
        timeout_seconds=timeout,
    )
    return result, seen, paths, instance


# --- tools and prompts ---------------------------------------------------------------------

def test_brainstorm_profile_exposes_only_read_tools(tmp_path):
    _result, seen, _paths, instance = run_stage(tmp_path)
    request = seen[0]
    assert request["allowed_tools"] == ["Read", "Glob", "Grep"]
    options = instance._sdk_option_kwargs(request)
    assert options["allowed_tools"] == ["Read", "Glob", "Grep"]
    assert options["tools"] == ["Read", "Glob", "Grep"]
    assert {"Write", "Edit", "Bash", "WebFetch", "WebSearch", "NotebookEdit"} <= set(options["disallowed_tools"])
    assert options["setting_sources"] == [] and options["skills"] == []
    assert options["strict_mcp_config"] is True
    assert options["permission_mode"] == "dontAsk"
    assert options["output_format"] == {"type": "json_schema", "schema": STAGE_SCHEMAS["proposals"]}


def test_hermes_profile_options_are_unchanged(tmp_path):
    instance = adapter(tmp_path)
    request = {"cwd": "/w", "authorized_roots": ["/w"], "task_file": "/w/t.md", "task_text": "x",
               "profile": "hermes", "allowed_tools": ["Read", "Glob", "Grep", "Write", "Edit"],
               "timeout_seconds": 5, "max_turns": 3, "result_schema": {"type": "object"}}
    options = instance._sdk_option_kwargs(request)
    assert options["allowed_tools"] == ["Read", "Glob", "Grep", "Write", "Edit"]
    assert options["disallowed_tools"] == ["Bash", "WebFetch", "WebSearch", "NotebookEdit"]
    assert "tools" not in options and "strict_mcp_config" not in options
    system_prompt, prompt = ClaudeAgentAdapter._build_prompt(request)
    assert system_prompt == "You are a Hermes execution worker. Follow the explicit tool and path policy exactly."
    assert prompt.startswith("Edits are allowed only through Write/Edit inside authorized roots.")


def test_claude_smoke_prompt_is_unchanged():
    request = {"profile": "claude_smoke", "task_file": "/w/t.md", "task_text": "x"}
    _system, prompt = ClaudeAgentAdapter._build_prompt(request)
    assert prompt == (
        "Read-only smoke validation. Do not modify files.\n"
        "Follow the task snapshot below. Do not use tools outside the allow-list. "
        "Return the final result through the required structured-output schema.\n\n"
        "TASK FILE: /w/t.md\n\nx"
    )


def test_brainstorm_prompt_is_read_only_and_treats_inputs_as_untrusted(tmp_path):
    _result, seen, _paths, _instance = run_stage(tmp_path, stage="evaluation")
    system_prompt, prompt = ClaudeAgentAdapter._build_prompt(seen[0])
    combined = system_prompt + prompt
    assert "Edits are allowed" not in combined
    assert "read-only" in combined
    assert "untrusted data" in prompt
    assert "Hermes brainstorm stage: evaluation" in prompt
    assert "only the object required by the structured-output schema" in prompt
    assert "Propose formats." in prompt


# --- stage roots ---------------------------------------------------------------------------------

def test_request_uses_exact_stage_roots_not_global_roots(tmp_path):
    _result, seen, paths, _instance = run_stage(tmp_path)
    assert seen[0]["authorized_roots"] == [str(paths["repo"]), str(paths["stage"])]
    assert str(tmp_path) not in seen[0]["authorized_roots"]


def test_stage_roots_do_not_reach_sibling_stage_or_orchestration(tmp_path):
    _result, seen, paths, instance = run_stage(tmp_path)
    roots = [Path(item) for item in seen[0]["authorized_roots"]]
    cwd = Path(seen[0]["cwd"])
    tools = seen[0]["allowed_tools"]
    assert instance._tool_input_authorized("Read", {"file_path": str(paths["stage"] / "task.md")}, cwd, roots, tools)
    assert instance._tool_input_authorized("Grep", {"path": str(paths["repo"])}, cwd, roots, tools)
    assert not instance._tool_input_authorized("Read", {"file_path": str(paths["sibling"] / "proposals.json")}, cwd, roots, tools)
    assert not instance._tool_input_authorized("Read", {"file_path": str(paths["orchestration"] / "baseline.json")}, cwd, roots, tools)
    assert not instance._tool_input_authorized("Glob", {"path": str(paths["job"]), "pattern": "**/*.json"}, cwd, roots, tools)
    assert not instance._tool_input_authorized("Read", {"file_path": "../runtime/job-1/stages/codex-proposals/proposals.json"}, cwd, roots, tools)


def test_hook_rejects_write_tools_for_brainstorm_even_inside_roots(tmp_path):
    _result, seen, paths, instance = run_stage(tmp_path)
    roots = [Path(item) for item in seen[0]["authorized_roots"]]
    target = {"file_path": str(paths["stage"] / "out.md")}
    assert not instance._tool_input_authorized("Write", target, Path(seen[0]["cwd"]), roots, seen[0]["allowed_tools"])
    assert not instance._tool_input_authorized("Edit", target, Path(seen[0]["cwd"]), roots, seen[0]["allowed_tools"])


@pytest.mark.parametrize("roots_factory, message", [
    (lambda p, root: [], "stage_roots"),
    (lambda p, root: "not-a-list", "stage_roots"),
    (lambda p, root: [str(p["repo"]), str(root.parent / "elsewhere")], "outside authorized roots"),
    (lambda p, root: [str(p["stage"])], "working_directory"),
    (lambda p, root: [str(p["repo"]), str(p["sibling"])], "brain.task_file"),
])
def test_invalid_stage_roots_are_blocked(tmp_path, roots_factory, message):
    paths = layout(tmp_path)
    instance = adapter(tmp_path, lambda _request: PROPOSALS)
    with pytest.raises(StructuredExecutionError, match=message) as info:
        instance.execute_structured(brainstorm_task(paths), stage_schema="proposals",
                                    stage_roots=roots_factory(paths, tmp_path), timeout_seconds=60)
    assert info.value.status == "BLOCKED"


def test_run_output_dir_must_be_inside_stage_roots(tmp_path):
    paths = layout(tmp_path)
    instance = adapter(tmp_path, lambda _request: PROPOSALS)
    with pytest.raises(StructuredExecutionError, match="run_output_dir"):
        instance.execute_structured(brainstorm_task(paths, run_output_dir=str(paths["orchestration"])),
                                    stage_schema="proposals",
                                    stage_roots=[str(paths["repo"]), str(paths["stage"])], timeout_seconds=60)


# --- schemas --------------------------------------------------------------------------------------

@pytest.mark.parametrize("stage", ["proposals", "evaluation", "refinement", "validation"])
def test_schema_is_resolved_by_name_from_stage_schemas(tmp_path, stage):
    _result, seen, _paths, _instance = run_stage(tmp_path, stage=stage)
    assert seen[0]["result_schema"] == STAGE_SCHEMAS[stage]
    assert seen[0]["result_schema"] is not STAGE_SCHEMAS[stage]
    assert seen[0]["stage"] == stage


@pytest.mark.parametrize("stage_schema", ["report", "", "PROPOSALS", "../proposals", {"type": "object"}, None])
def test_unknown_or_arbitrary_schema_is_blocked(tmp_path, stage_schema):
    paths = layout(tmp_path)
    called = []
    instance = adapter(tmp_path, lambda request: called.append(request) or PROPOSALS)
    with pytest.raises(StructuredExecutionError, match="stage schema") as info:
        instance.execute_structured(brainstorm_task(paths), stage_schema=stage_schema,
                                    stage_roots=[str(paths["repo"]), str(paths["stage"])], timeout_seconds=60)
    assert info.value.status == "BLOCKED"
    assert called == []


def test_task_cannot_inject_a_schema(tmp_path):
    injected = {"type": "object", "properties": {"pwned": {"type": "string"}}}
    _result, seen, _paths, _instance = run_stage(tmp_path, task_overrides={
        "result_schema": injected, "output_schema": injected, "stage_schema": "evil",
    })
    assert seen[0]["result_schema"] == STAGE_SCHEMAS["proposals"]


def test_execute_structured_requires_keyword_arguments(tmp_path):
    paths = layout(tmp_path)
    instance = adapter(tmp_path, lambda _request: PROPOSALS)
    with pytest.raises(TypeError):
        instance.execute_structured(brainstorm_task(paths), "proposals", [str(paths["repo"])], 60)  # type: ignore[misc]


# --- timeout ----------------------------------------------------------------------------------------

def test_effective_timeout_is_propagated_instead_of_task_timeout(tmp_path):
    _result, seen, _paths, _instance = run_stage(tmp_path, timeout=42)
    assert seen[0]["timeout_seconds"] == 42


@pytest.mark.parametrize("timeout", [0, -1, 7201, True, "60", 1.5, None])
def test_invalid_effective_timeout_is_blocked(tmp_path, timeout):
    with pytest.raises(StructuredExecutionError, match="timeout_seconds"):
        run_stage(tmp_path, timeout=timeout)


# --- results and errors ---------------------------------------------------------------------------------

def test_structured_payload_is_returned_untransformed(tmp_path):
    payload = {"proposals": [{"title": "x", "status": "not-hermes"}], "extra": [1, 2]}
    result, _seen, paths, _instance = run_stage(tmp_path, payload=payload)
    assert isinstance(result, StructuredExecutionResult)
    assert result.stage == "proposals"
    assert result.payload == payload
    assert not hasattr(result, "status") and not hasattr(result, "result")
    log = paths["stage"] / "claude-structured.log"
    assert result.evidence == [str(log)]
    logged = json.loads(log.read_text(encoding="utf-8"))
    assert logged["stage"] == "proposals" and logged["payload"] == payload
    assert logged["allowed_tools"] == ["Read", "Glob", "Grep"]
    assert result.metadata["timeout_seconds"] == 60


@pytest.mark.parametrize("error, status, message", [
    (AdapterExecutionError("FAILED", "Claude returned an error result: error_max_structured_output_retries"),
     "FAILED", "error_max_structured_output_retries"),
    (AdapterExecutionError("BLOCKED", "claude-agent-sdk is not available"), "BLOCKED", "not available"),
    (TimeoutError(), "FAILED", "timed out"),
    (RuntimeError("boom"), "FAILED", "RuntimeError"),
])
def test_runner_errors_propagate_as_structured_errors(tmp_path, error, status, message):
    def runner(_request):
        raise error

    paths = layout(tmp_path)
    instance = adapter(tmp_path, runner)
    with pytest.raises(StructuredExecutionError, match=message) as info:
        instance.execute_structured(brainstorm_task(paths), stage_schema="proposals",
                                    stage_roots=[str(paths["repo"]), str(paths["stage"])], timeout_seconds=60)
    assert info.value.status == status
    assert info.value.stage == "proposals"
    log = paths["stage"] / "claude-structured.log"
    assert info.value.evidence == [str(log)]
    assert json.loads(log.read_text(encoding="utf-8"))["error"]


@pytest.mark.parametrize("payload", [["not", "an", "object"], "text", None])
def test_non_object_structured_output_fails(tmp_path, payload):
    with pytest.raises(StructuredExecutionError, match="JSON object") as info:
        run_stage(tmp_path, payload=payload)
    assert info.value.status == "FAILED"


# --- profile gating and subscription policy -----------------------------------------------------------------

def test_brainstorm_profile_must_be_enabled_on_adapter(tmp_path):
    paths = layout(tmp_path)
    instance = ClaudeAgentAdapter(authorized_roots=[tmp_path], runner=lambda _request: PROPOSALS)
    with pytest.raises(StructuredExecutionError, match="execution_profile"):
        instance.execute_structured(brainstorm_task(paths), stage_schema="proposals",
                                    stage_roots=[str(paths["repo"]), str(paths["stage"])], timeout_seconds=60)


@pytest.mark.parametrize("overrides, message", [
    ({"execution_profile": "hermes"}, "execution_profile"),
    ({"task_type": "development"}, "task_type"),
])
def test_structured_route_requires_brainstorm_task(tmp_path, overrides, message):
    with pytest.raises(StructuredExecutionError, match=message):
        run_stage(tmp_path, task_overrides=overrides)


def test_execute_rejects_brainstorm_profile(tmp_path):
    paths = layout(tmp_path)
    called = []
    instance = adapter(tmp_path, lambda request: called.append(request) or PROPOSALS)
    result = instance.execute(brainstorm_task(paths))
    assert result.status == "BLOCKED"
    assert "execute_structured" in result.summary
    assert called == []


@pytest.mark.parametrize("variable", ["ANTHROPIC_API_KEY", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY"])
def test_subscription_only_blocks_external_providers_on_structured_route(tmp_path, monkeypatch, variable):
    monkeypatch.setenv(variable, "should-not-be-used")
    paths = layout(tmp_path)
    instance = adapter(tmp_path, cli_path=tmp_path / "claude")
    (tmp_path / "claude").write_text("", encoding="utf-8")
    with pytest.raises(StructuredExecutionError, match="subscription-only") as info:
        instance.execute_structured(brainstorm_task(paths), stage_schema="proposals",
                                    stage_roots=[str(paths["repo"]), str(paths["stage"])], timeout_seconds=60)
    assert info.value.status == "BLOCKED"


def test_subscription_only_requires_cli_path_on_structured_route(tmp_path, monkeypatch):
    for variable in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY"):
        monkeypatch.delenv(variable, raising=False)
    paths = layout(tmp_path)
    with pytest.raises(StructuredExecutionError, match="authenticated Claude CLI path") as info:
        adapter(tmp_path).execute_structured(brainstorm_task(paths), stage_schema="proposals",
                                             stage_roots=[str(paths["repo"]), str(paths["stage"])], timeout_seconds=60)
    assert info.value.status == "BLOCKED"
