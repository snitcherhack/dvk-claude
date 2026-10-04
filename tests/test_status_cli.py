from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "result,gate,expected",
    [
        ({"status": "DONE", "summary": "All done"}, None,
         "result=DONE summary=All done gate=none"),
        (None, None, "result=- summary=- gate=none"),
        ({"status": "WAIT_USER", "summary": "Needs approval"},
         {"waiting_for": "REVIEW"},
         "result=WAIT_USER summary=Needs approval gate=REVIEW"),
        ({}, {}, "result=- summary=- gate=none"),
    ],
)
def test_render_status_compact(result, gate, expected):
    from hermes_controller.__main__ import render_status

    status = {"state": "DONE", "attempt": 2, "result": result, "gate": gate}
    assert render_status(status, compact=True) == f"state=DONE attempt=2 {expected}"


def test_render_status_compact_is_one_line():
    from hermes_controller.__main__ import render_status

    status = {"state": "DONE", "attempt": 1,
              "result": {"status": "DONE", "summary": "First\nSecond\r\nThird"},
              "gate": None}
    assert render_status(status, compact=True) == (
        "state=DONE attempt=1 result=DONE summary=First Second Third gate=none"
    )


@pytest.mark.parametrize("result", [None, {"status": "DONE", "summary": "Listo"}])
def test_render_status_fields(result):
    from hermes_controller.__main__ import render_status

    status = {"state": "DONE", "result": result, "gate": None,
              "nested": {"a": {"b": 0}}}
    fields = ["state", "result.status", "result.summary", "gate.waiting_for",
              "missing", "nested.a.b", "state.invalid"]
    assert json.loads(render_status(status, fields=fields)) == {
        "state": "DONE", "result.status": result["status"] if result else None,
        "result.summary": result["summary"] if result else None,
        "gate.waiting_for": None, "missing": None, "nested.a.b": 0,
        "state.invalid": None,
    }


def test_render_status_default_preserves_json():
    from hermes_controller.__main__ import render_status

    status = {"state": "DONE", "result": {"summary": "acción"}, "attempt": 1}
    assert render_status(status) == json.dumps(status, sort_keys=True)


def test_status_cli(tmp_path):
    repo = Path(__file__).parents[1]
    manifest = {
        "project_id": "status-cli", "repository": "git@example/status.git",
        "ref": "main", "platform": "linux",
        "working_directory": str(tmp_path / "workspace"),
        "runtime_directory": str(tmp_path / "runs"),
        "allowed_engines": ["codex"], "default_engine": "codex",
        "capabilities": ["git"],
    }
    manifest_file = tmp_path / "project.json"
    manifest_file.write_text(json.dumps(manifest), encoding="utf-8")

    def run(*args):
        return subprocess.run(
            [sys.executable, "-m", "hermes_controller", "--runtime-root",
             str(tmp_path / "controller"), *args],
            cwd=repo, text=True, capture_output=True, check=True,
        ).stdout

    run("project", "register", str(manifest_file))
    job = json.loads(run("task", "create", "status-cli", "--instruction", "Test status"))
    job_id = job["job_id"]
    default = run("status", job_id)
    assert default == json.dumps(json.loads(default), sort_keys=True) + "\n"
    assert run("status", job_id, "--compact") == (
        "state=QUEUED attempt=0 result=- summary=- gate=none\n"
    )
    assert json.loads(run("status", job_id, "--fields", "state,result.status")) == {
        "state": "QUEUED", "result.status": None,
    }
