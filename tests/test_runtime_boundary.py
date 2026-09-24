"""Inline-task runtimes must never live inside the working directory (Controller + Worker)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from hermes_controller.adapters import AdapterResult
from hermes_controller.controller import Controller, ControllerError
from hermes_controller.worker import WorkerDaemon

REPO = Path(__file__).parents[1]
PROJECTS = REPO / "config" / "hermes-projects"
WORKER = {"worker_id": "main-linux", "platform": "linux", "environment": "t", "capabilities": ["codex"],
          "max_concurrent_jobs": 1}


def manifest(working_directory: str, runtime_directory: str, **overrides) -> dict:
    spec = {"project_id": "sample", "repository": "git@example/sample.git", "ref": "main", "platform": "linux",
            "working_directory": working_directory, "runtime_directory": runtime_directory,
            "allowed_engines": ["codex"], "default_engine": "codex", "capabilities": ["git"]}
    spec.update(overrides)
    return spec


# --- Controller manifest validation --------------------------------------------------------------------------------

@pytest.mark.parametrize("working, runtime", [
    ("/repo", "/repo"),
    ("/repo", "/repo/"),
    ("/repo/", "/repo"),
    ("/repo", "/repo/.hermes"),
    ("/repo", "/repo/runtime/jobs"),
    ("/repo", "/repo/./runtime"),
    ("/home/deiv/Proyectos/app", "/home/deiv/Proyectos/other/../app/state"),
    ("/", "/var/lib/hermes"),
])
def test_manifest_rejects_runtime_inside_working_directory(tmp_path, working, runtime):
    controller = Controller(tmp_path / "controller")
    with pytest.raises(ControllerError, match="runtime_directory"):
        controller.register_project(manifest(working, runtime))
    controller.close()


@pytest.mark.parametrize("working, runtime", [
    ("/home/deiv/Proyectos/proyecto", "/home/deiv/.local/state/dvk-hermes-projects/proyecto"),
    ("/repo", "/repository-runtime"),
    ("/repo/app", "/repo/app-runtime"),
    ("/repo/app", "/repo"),
])
def test_manifest_accepts_runtime_outside_working_directory(tmp_path, working, runtime):
    controller = Controller(tmp_path / "controller")
    assert controller.register_project(manifest(working, runtime)) == "sample"
    controller.close()


def test_versioned_project_manifests_remain_valid():
    paths = sorted(PROJECTS.glob("*.json"))
    assert paths
    for path in paths:
        Controller._validate_project(json.loads(path.read_text(encoding="utf-8")))


# --- Worker materialization ------------------------------------------------------------------------------------------

def inline_task(working_directory: Path, run_output_dir: Path) -> dict:
    text = "Inline task."
    return {
        "brain": {"repository": "inline://hermes-projects/sample", "ref": "sample",
                  "commit": hashlib.sha256(text.encode("utf-8")).hexdigest()},
        "task_text": text, "working_directory": str(working_directory), "run_output_dir": str(run_output_dir),
    }


def worker(tmp_path, monkeypatch, *roots: Path) -> WorkerDaemon:
    monkeypatch.setenv("HERMES_TASK_ROOTS_BOUNDARY", ":".join(str(root) for root in roots))
    return WorkerDaemon({"controller_url": "http://127.0.0.1:9", "token": "t", "state_file": str(tmp_path / "s.json"),
                         "task_materialization_roots_env": "HERMES_TASK_ROOTS_BOUNDARY", "worker": WORKER},
                        adapter=type("Never", (), {"execute": lambda self, task: AdapterResult()})())


def test_worker_refuses_to_materialize_inside_the_repo(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    daemon = worker(tmp_path, monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="working_directory"):
        daemon._materialize_inline_task(inline_task(repo, repo / "runtime" / "job"))
    assert not (repo / "runtime" / "job" / "hermes-task.md").exists()
    assert not (repo / "runtime").exists()


def test_worker_refuses_run_output_equal_to_the_repo(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    daemon = worker(tmp_path, monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="working_directory"):
        daemon._materialize_inline_task(inline_task(repo, repo))
    assert not (repo / "hermes-task.md").exists()


def test_worker_resolves_symlinks_before_checking(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    alias = tmp_path / "runtime-alias"
    alias.symlink_to(repo, target_is_directory=True)
    daemon = worker(tmp_path, monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="working_directory"):
        daemon._materialize_inline_task(inline_task(repo, alias / "job"))
    assert not (repo / "job").exists()


def test_worker_materializes_outside_the_repo(tmp_path, monkeypatch):
    repo, runtime = tmp_path / "repo", tmp_path / "runtime"
    repo.mkdir()
    daemon = worker(tmp_path, monkeypatch, runtime)
    prepared = daemon._materialize_inline_task(inline_task(repo, runtime / "job"))
    assert Path(prepared["brain"]["task_file"]) == runtime / "job" / "hermes-task.md"
    assert (runtime / "job" / "hermes-task.md").read_text(encoding="utf-8") == "Inline task."
    assert list(repo.iterdir()) == []


def test_boundary_violation_becomes_blocked_result(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    daemon = worker(tmp_path, monkeypatch, tmp_path)
    claim = {"job_id": "j", "run_id": "r", "attempt": 1, "lease_id": "l", "lease_token": "t",
             "task": inline_task(repo, repo / "runtime" / "job")}
    with pytest.raises(ValueError, match="working_directory"):
        daemon._prepare_task(claim)
    assert list(repo.iterdir()) == []


# --- inline tasks without working_directory fail closed ------------------------------------------------------------

@pytest.mark.parametrize("working_directory", [None, "", 7])
def test_inline_task_without_working_directory_writes_nothing(tmp_path, monkeypatch, working_directory):
    runtime = tmp_path / "runtime"
    daemon = worker(tmp_path, monkeypatch, runtime)
    task = inline_task(tmp_path / "repo", runtime / "job")
    if working_directory is None:
        task.pop("working_directory")
    else:
        task["working_directory"] = working_directory
    with pytest.raises(ValueError, match="inline tasks require working_directory"):
        daemon._materialize_inline_task(task)
    assert not (runtime / "job").exists()
    assert not (runtime / "job" / "hermes-task.md").exists()
    claim = {"job_id": "j", "run_id": "r", "attempt": 1, "lease_id": "l", "lease_token": "t", "task": task}
    with pytest.raises(ValueError, match="inline tasks require working_directory"):
        daemon._prepare_task(claim)
    assert not runtime.exists() or list(runtime.iterdir()) == []


def test_once_blocks_inline_task_without_working_directory_without_calling_adapter(tmp_path, monkeypatch):
    import threading

    from hermes_controller.api import serve

    runtime = tmp_path / "runtime"
    monkeypatch.setenv("HERMES_TASK_ROOTS_BOUNDARY", str(runtime))
    controller = Controller(tmp_path / "controller")
    server = serve(controller, port=0, enrollment_tokens={"main-linux": "boundary-token"})
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    calls = []
    try:
        daemon = WorkerDaemon({"controller_url": f"http://127.0.0.1:{server.server_port}", "token": "boundary-token",
                               "state_file": str(tmp_path / "state.json"), "heartbeat_interval_ms": 10,
                               "task_materialization_roots_env": "HERMES_TASK_ROOTS_BOUNDARY", "worker": WORKER},
                              adapter=type("Spy", (), {"execute": lambda self, task: calls.append(task) or AdapterResult()})())
        daemon.register()
        task = inline_task(tmp_path / "repo", runtime / "job")
        task.pop("working_directory")
        task.update({"project": "sample", "repository": "git@example/sample.git", "ref": "main",
                     "task_type": "development", "platform": "linux", "required_capabilities": ["codex"],
                     "execution_profile": "hermes", "human_gates": [], "idempotency_policy": "safe_retry"})
        job_id = controller.enqueue(task)
        assert daemon.once()
        status = controller.status(job_id)
        assert status["state"] == "BLOCKED"
        assert "inline tasks require working_directory" in status["result"]["summary"]
        assert calls == []
        assert not (runtime / "job").exists()
    finally:
        server.shutdown()
        server.server_close()
        controller.close()
