from __future__ import annotations

from pathlib import Path

import pytest

from hermes_controller.adapters import ClaudeAgentAdapter


def hermes_result(**overrides):
    result = {
        "status": "DONE",
        "summary": "claude ok",
        "gate": None,
        "completed": ["checked"],
        "remaining": [],
        "evidence": ["fake-evidence"],
    }
    result.update(overrides)
    return result


def task(root: Path, **overrides):
    work, brain, out = root / "repo", root / "brain", root / "runtime"
    work.mkdir(exist_ok=True)
    (work / ".git").mkdir(exist_ok=True)
    brain.mkdir(exist_ok=True)
    (brain / "task.md").write_text("Inspect the repository and report status.", encoding="utf-8")
    spec = {
        "task_type": "claude_smoke",
        "execution_profile": "claude_smoke",
        "working_directory": str(work),
        "brain": {"task_file": str(brain / "task.md")},
        "run_output_dir": str(out),
        "timeout_seconds": 3,
        "allowed_paths": [str(work), str(brain), str(out)],
    }
    spec.update(overrides)
    return spec


def test_claude_smoke_is_read_only_and_returns_done(tmp_path):
    seen = []
    adapter = ClaudeAgentAdapter(
        authorized_roots=[tmp_path],
        runner=lambda request: (seen.append(request) or hermes_result()),
    )
    result = adapter.execute(task(tmp_path))
    assert result.status == "DONE"
    assert seen[0]["allowed_tools"] == ["Read", "Glob", "Grep"]
    assert "Bash" not in seen[0]["allowed_tools"]
    assert any("claude-agent.log" in item for item in result.evidence)


def test_claude_hermes_profile_allows_scoped_edits_but_not_bash(tmp_path):
    seen = []
    adapter = ClaudeAgentAdapter(
        authorized_roots=[tmp_path],
        runner=lambda request: (seen.append(request) or hermes_result()),
    )
    result = adapter.execute(task(
        tmp_path, task_type="development", execution_profile="hermes"
    ))
    assert result.status == "DONE"
    assert seen[0]["allowed_tools"] == ["Read", "Glob", "Grep", "Write", "Edit"]
    assert "Bash" not in seen[0]["allowed_tools"]


def test_claude_adapter_blocks_paths_outside_roots(tmp_path):
    adapter = ClaudeAgentAdapter(
        authorized_roots=[tmp_path],
        runner=lambda _request: hermes_result(),
    )
    result = adapter.execute(task(tmp_path, working_directory="/tmp"))
    assert result.status == "BLOCKED"
    assert "outside authorized roots" in result.summary


def test_claude_adapter_fails_closed_on_invalid_result(tmp_path):
    adapter = ClaudeAgentAdapter(
        authorized_roots=[tmp_path],
        runner=lambda _request: {"status": "DONE"},
    )
    result = adapter.execute(task(tmp_path))
    assert result.status == "FAILED"
    assert "result contract" in result.summary


def test_claude_adapter_timeout_is_failed(tmp_path):
    def timeout(_request):
        raise TimeoutError()

    adapter = ClaudeAgentAdapter(authorized_roots=[tmp_path], runner=timeout)
    result = adapter.execute(task(tmp_path))
    assert result.status == "FAILED"
    assert "timed out" in result.summary


@pytest.mark.parametrize(
    "variable",
    ["ANTHROPIC_API_KEY", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY"],
)
def test_subscription_only_blocks_external_provider_config_before_sdk_use(tmp_path, monkeypatch, variable):
    monkeypatch.setenv(variable, "should-not-be-used")
    adapter = ClaudeAgentAdapter(authorized_roots=[tmp_path])
    result = adapter.execute(task(tmp_path))
    assert result.status == "BLOCKED"
    assert "subscription-only" in result.summary
    assert variable in result.summary


def test_subscription_only_requires_authenticated_cli_path(tmp_path):
    adapter = ClaudeAgentAdapter(authorized_roots=[tmp_path])
    result = adapter.execute(task(tmp_path))
    assert result.status == "BLOCKED"
    assert "Claude CLI path" in result.summary


def test_configured_cli_path_must_exist(tmp_path):
    adapter = ClaudeAgentAdapter(authorized_roots=[tmp_path], cli_path=tmp_path / "missing-claude")
    result = adapter.execute(task(tmp_path))
    assert result.status == "BLOCKED"
    assert "CLI path is not available" in result.summary


def test_path_guard_accepts_only_authorized_roots(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    cwd = root / "repo"
    cwd.mkdir()
    adapter = ClaudeAgentAdapter(
        authorized_roots=[root],
        runner=lambda _request: hermes_result(),
    )
    assert adapter._tool_input_authorized("Read", {"file_path": "README.md"}, cwd)
    assert adapter._tool_input_authorized("Glob", {"pattern": "**/*.py"}, cwd)
    assert not adapter._tool_input_authorized("Read", {"file_path": "/etc/passwd"}, cwd)
    assert not adapter._tool_input_authorized("Glob", {"pattern": "../**/*"}, cwd)
    assert not adapter._tool_input_authorized("Bash", {"command": "pwd"}, cwd)
    sibling = root / "sibling"
    sibling.mkdir()
    assert not adapter._tool_input_authorized("Read", {"file_path": str(sibling / "secret.txt")}, cwd, [cwd])
