from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest

import hermes_controller.adapters as adapters
from hermes_controller.adapters import AdapterResult, CodexRunAdapter, CxhRunAdapter, EngineRoutingAdapter


def task(root: Path, **overrides):
    work, brain, out = root / "repo", root / "brain", root / "runtime"
    work.mkdir(exist_ok=True); (work / ".git").mkdir(exist_ok=True); brain.mkdir(exist_ok=True); (brain / "task.md").write_text("read only")
    return {"task_type": "development", "execution_profile": "hermes", "working_directory": str(work), "brain": {"task_file": str(brain / "task.md")}, "run_output_dir": str(out), "timeout_seconds": 3, "allowed_paths": [str(work), str(brain)], **overrides}


class Process:
    def __init__(self, argv, **_kwargs): self.argv = argv
    def wait(self, timeout=None): return 0
    def terminate(self): pass
    def kill(self): pass


def adapter(root):
    runner = root / "cxh-run"; runner.write_text("runner")
    return CxhRunAdapter(runner_path=runner, authorized_roots=[root])


def codex_adapter(root):
    runner = root / "hermes-codex-run"; runner.write_text("runner")
    return CodexRunAdapter(runner_path=runner, authorized_roots=[root])


def write_result(spec):
    out = Path(spec["run_output_dir"]); out.mkdir(exist_ok=True)
    (out / "result.json").write_text(json.dumps({"status": "DONE", "summary": "ok", "gate": None, "completed": [], "remaining": [], "evidence": []}))


def test_a_argv_has_no_shell_and_h_done_is_wrapped(tmp_path, monkeypatch):
    spec = task(tmp_path); write_result(spec); seen = []
    monkeypatch.setattr(adapters.subprocess, "Popen", lambda argv, **kw: (seen.append((argv, kw)) or Process(argv, **kw)))
    result = adapter(tmp_path).execute(spec)
    assert result.status == "DONE" and seen[0][1]["shell"] is False and "--working-directory" in seen[0][0]
    assert "--smoke-test" not in seen[0][0]


def test_hermes_smoke_passes_runner_smoke_flag(tmp_path, monkeypatch):
    spec = task(tmp_path, task_type="hermes_smoke"); write_result(spec); seen = []
    monkeypatch.setattr(adapters.subprocess, "Popen", lambda argv, **kw: (seen.append(argv) or Process(argv, **kw)))
    result = adapter(tmp_path).execute(spec)
    assert result.status == "DONE" and "--smoke-test" in seen[0]


def test_engine_router_selects_explicit_engine_and_preserves_default():
    class TaggedAdapter:
        def __init__(self, name): self.name = name
        def execute(self, _task): return AdapterResult(summary=self.name)

    router = EngineRoutingAdapter({"codex": TaggedAdapter("codex"), "claude": TaggedAdapter("claude")})
    assert router.execute({}).summary == "codex"
    assert router.execute({"execution_engine": "claude"}).summary == "claude"
    blocked = router.execute({"execution_engine": "hybrid"})
    assert blocked.status == "BLOCKED" and "not configured" in blocked.summary
    native = router.execute({"required_capabilities": ["image_qa"]})
    assert native.status == "BLOCKED" and "native" in native.summary


def test_b_c_k_reject_invalid_root_profile_and_task_type(tmp_path):
    instance = adapter(tmp_path)
    assert instance.execute(task(tmp_path, working_directory="/outside")).status == "BLOCKED"
    assert instance.execute(task(tmp_path, execution_profile="unknown")).status == "BLOCKED"
    assert instance.execute(task(tmp_path, task_type="arbitrary_shell")).status == "BLOCKED"


def test_d_f_invalid_or_missing_result_is_controlled(tmp_path, monkeypatch):
    spec = task(tmp_path); monkeypatch.setattr(adapters.subprocess, "Popen", Process)
    assert adapter(tmp_path).execute(spec).status == "FAILED"
    Path(spec["run_output_dir"]).mkdir(exist_ok=True); (Path(spec["run_output_dir"]) / "result.json").write_text("[]")
    assert adapter(tmp_path).execute(spec).status == "FAILED"


def test_g_i_logs_and_wait_user_gate_are_evidence(tmp_path, monkeypatch):
    spec = task(tmp_path); write_result(spec)
    path = Path(spec["run_output_dir"]) / "result.json"; data = json.loads(path.read_text()); data.update(status="WAIT_USER", gate="WINDOWS_FINAL_RENDER_REQUIRED"); path.write_text(json.dumps(data))
    monkeypatch.setattr(adapters.subprocess, "Popen", Process)
    result = adapter(tmp_path).execute(spec)
    assert result.status == "WAIT_USER" and result.gate == "WINDOWS_FINAL_RENDER_REQUIRED" and any("cxh-run.log" in x for x in result.evidence)


def test_e_timeout_returns_failed(tmp_path, monkeypatch):
    spec = task(tmp_path)
    class Slow(Process):
        def wait(self, timeout=None):
            if timeout == 13: raise adapters.subprocess.TimeoutExpired("cxh", timeout)
            return 0
    monkeypatch.setattr(adapters.subprocess, "Popen", Slow)
    assert adapter(tmp_path).execute(spec).status == "FAILED"


def test_runner_help_exits_before_runtime_setup():
    runner = Path(__file__).parents[1] / "cxh-run.sh"
    result = subprocess.run([runner, "--help"], text=True, capture_output=True, check=False)
    assert result.returncode == 0
    assert "Uso: cxh-run.sh" in result.stdout


def test_runner_dry_run_is_reachable_and_has_no_state_side_effects(tmp_path):
    runner = Path(__file__).parents[1] / "cxh-run.sh"
    brain = tmp_path / "brain"
    task_file = brain / "task.md"
    task_file.parent.mkdir()
    task_file.write_text("read only")
    work = tmp_path / "repo"
    work.mkdir()
    state_home = tmp_path / "state-home"
    environment = {**os.environ, "HERMES_BRAIN_DIR": str(brain), "XDG_STATE_HOME": str(state_home)}
    result = subprocess.run(
        [runner, "--dry-run", "--working-directory", str(work), "--task-file", str(task_file)],
        text=True, capture_output=True, env=environment, check=False,
    )
    assert result.returncode == 0
    assert f"cwd: {work}" in result.stdout
    assert "command: codex exec" in result.stdout
    assert not state_home.exists()


def test_codex_run_adapter_passes_task_scoped_allowed_paths(tmp_path, monkeypatch):
    spec = task(tmp_path)
    write_result(spec)
    seen = []
    monkeypatch.setattr(adapters.subprocess, "Popen", lambda argv, **kw: (seen.append(argv) or Process(argv, **kw)))
    result = codex_adapter(tmp_path).execute(spec)
    assert result.status == "DONE"
    assert any("codex-run.log" in item for item in result.evidence)
    argv = seen[0]
    allowed = [argv[index + 1] for index, value in enumerate(argv) if value == "--allowed-path"]
    assert allowed == [str(Path(spec["working_directory"]).resolve()), str((tmp_path / "brain").resolve())]


def test_project_agnostic_runner_help():
    runner = Path(__file__).parents[1] / "hermes-codex-run.sh"
    result = subprocess.run([runner, "--help"], text=True, capture_output=True, check=False)
    assert result.returncode == 0
    assert "Uso: hermes-codex-run.sh" in result.stdout
    assert "--allowed-path" in result.stdout


def test_project_agnostic_runner_dry_run_has_no_video_dependency(tmp_path):
    runner = Path(__file__).parents[1] / "hermes-codex-run.sh"
    work = tmp_path / "repo"
    task_dir = tmp_path / "tasks"
    out = tmp_path / "runtime"
    work.mkdir()
    (work / ".git").mkdir()
    task_dir.mkdir()
    task_file = task_dir / "task.md"
    task_file.write_text("generic task")
    state_home = tmp_path / "state-home"
    environment = {**os.environ, "XDG_STATE_HOME": str(state_home)}
    result = subprocess.run(
        [
            runner, "--dry-run",
            "--working-directory", str(work),
            "--task-file", str(task_file),
            "--run-output-dir", str(out),
            "--allowed-path", str(work),
            "--allowed-path", str(task_dir),
            "--allowed-path", str(out),
        ],
        text=True, capture_output=True, env=environment, check=False,
    )
    assert result.returncode == 0
    assert f"task: {task_file}" in result.stdout
    assert "Winner Timeline" not in result.stdout
    assert "youtube" not in result.stdout.lower()
    assert not state_home.exists()


def test_project_agnostic_runner_rejects_path_outside_task_scope(tmp_path):
    runner = Path(__file__).parents[1] / "hermes-codex-run.sh"
    work = tmp_path / "repo"
    task_dir = tmp_path / "tasks"
    out = tmp_path / "runtime"
    work.mkdir()
    (work / ".git").mkdir()
    task_dir.mkdir()
    task_file = task_dir / "task.md"
    task_file.write_text("generic task")
    result = subprocess.run(
        [
            runner, "--dry-run",
            "--working-directory", str(work),
            "--task-file", str(task_file),
            "--run-output-dir", str(out),
            "--allowed-path", str(work),
            "--allowed-path", str(out),
        ],
        text=True, capture_output=True, check=False,
    )
    assert result.returncode == 2
    assert "ruta fuera de allowed paths" in result.stderr
