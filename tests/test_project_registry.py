from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from hermes_controller.controller import Controller, ControllerError


def manifest(root: Path, **overrides):
    spec = {
        "project_id": "sample-project",
        "repository": "git@example/sample.git",
        "ref": "main",
        "platform": "linux",
        "worker_id": "main-linux",
        "working_directory": str(root / "projects" / "sample-project"),
        "runtime_directory": str(root / "runtime" / "sample-project"),
        "allowed_engines": ["codex", "claude", "hybrid"],
        "default_engine": "codex",
        "capabilities": ["git", "python", "tests"],
        "task_type": "development",
        "execution_profile": "hermes",
        "human_gates": [],
        "idempotency_policy": "safe_retry",
        "timeout_seconds": 900,
        "max_turns": 12,
    }
    spec.update(overrides)
    return spec


def test_project_registry_round_trip(tmp_path):
    controller = Controller(tmp_path / "controller")
    spec = manifest(tmp_path)
    assert controller.register_project(spec) == "sample-project"
    listed = controller.list_projects()
    assert len(listed) == 1
    assert listed[0]["manifest"] == spec
    assert controller.project("sample-project")["manifest"]["repository"] == "git@example/sample.git"

    updated = {**spec, "default_engine": "claude"}
    controller.register_project(updated)
    assert controller.project("sample-project")["manifest"]["default_engine"] == "claude"
    assert len(controller.list_projects()) == 1

    controller.remove_project("sample-project")
    assert controller.list_projects() == []
    with pytest.raises(ControllerError, match="unknown project"):
        controller.project("sample-project")
    controller.close()


def test_project_manifest_validation(tmp_path):
    controller = Controller(tmp_path / "controller")
    with pytest.raises(ControllerError, match="project_id"):
        controller.register_project(manifest(tmp_path, project_id="../escape"))
    with pytest.raises(ControllerError, match="default_engine"):
        controller.register_project(manifest(tmp_path, default_engine="native"))
    with pytest.raises(ControllerError, match="absolute Linux path"):
        controller.register_project(manifest(tmp_path, working_directory="relative/path"))
    controller.close()


def test_project_task_builder_creates_inline_immutable_snapshot(tmp_path):
    controller = Controller(tmp_path / "controller")
    controller.register_project(manifest(tmp_path))
    instruction = "Inspect the repository and fix the failing unit test."
    task = controller.build_project_task("sample-project", instruction, engine="hybrid")

    assert task["project"] == "sample-project"
    assert task["execution_engine"] == "hybrid"
    assert task["worker_id"] == "main-linux"
    assert set(["codex", "claude", "git", "python", "tests"]).issubset(task["required_capabilities"])
    assert task["task_text"] == instruction
    assert task["brain"]["repository"] == "inline://hermes-projects/sample-project"
    assert task["brain"]["commit"] == hashlib.sha256(instruction.encode("utf-8")).hexdigest()
    assert "task_file" not in task["brain"]
    assert task["run_output_dir"].startswith(manifest(tmp_path)["runtime_directory"] + "/")
    assert manifest(tmp_path)["working_directory"] in task["allowed_paths"]
    assert manifest(tmp_path)["runtime_directory"] in task["allowed_paths"]
    controller.close()


def test_project_task_builder_rejects_disallowed_engine_and_bad_inline_hash(tmp_path):
    controller = Controller(tmp_path / "controller")
    controller.register_project(manifest(tmp_path, allowed_engines=["codex"], default_engine="codex"))
    with pytest.raises(ControllerError, match="not allowed"):
        controller.build_project_task("sample-project", "do work", engine="claude")

    task = controller.build_project_task("sample-project", "do work")
    task["brain"]["commit"] = "0" * 64
    with pytest.raises(ControllerError, match="hash"):
        controller.enqueue(task)
    controller.close()


def test_target_worker_prevents_other_matching_worker_from_claiming(tmp_path):
    controller = Controller(tmp_path / "controller")
    a = {
        "worker_id": "main-linux",
        "platform": "linux",
        "environment": "test",
        "capabilities": ["codex", "git", "python", "tests"],
        "max_concurrent_jobs": 1,
    }
    b = {**a, "worker_id": "other-linux"}
    controller.register_worker(a)
    controller.register_worker(b)
    controller.register_project(manifest(tmp_path, allowed_engines=["codex"], default_engine="codex"))
    task = controller.build_project_task("sample-project", "read only")
    job = controller.enqueue(task)

    assert controller.claim("other-linux") is None
    assert controller.claim("main-linux")["job_id"] == job
    controller.close()
