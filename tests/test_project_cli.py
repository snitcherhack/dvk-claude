from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_project_cli_register_and_build_multiline_instruction(tmp_path):
    repo = Path(__file__).parents[1]
    runtime = tmp_path / "controller"
    manifest = {
        "project_id": "cli-project",
        "repository": "git@example/cli.git",
        "ref": "main",
        "platform": "linux",
        "worker_id": "main-linux",
        "working_directory": "/home/deiv/Proyectos/cli-project",
        "runtime_directory": "/home/deiv/.local/state/dvk-hermes-projects/cli-project",
        "allowed_engines": ["codex", "hybrid"],
        "default_engine": "codex",
        "capabilities": ["git", "tests"],
    }
    manifest_file = tmp_path / "project.json"
    manifest_file.write_text(json.dumps(manifest), encoding="utf-8")
    instruction = "First line.\nSecond line with spaces.\n"
    instruction_file = tmp_path / "task.md"
    instruction_file.write_text(instruction, encoding="utf-8")
    output = tmp_path / "task.json"

    def run(*args):
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "hermes_controller",
                "--runtime-root",
                str(runtime),
                *args,
            ],
            cwd=repo,
            text=True,
            capture_output=True,
            check=True,
        )

    validated = json.loads(run("project", "validate", str(manifest_file)).stdout)
    assert validated == {"project_id": "cli-project", "valid": True}
    run("project", "register", str(manifest_file))
    listed = json.loads(run("project", "list").stdout)
    assert [item["project_id"] for item in listed] == ["cli-project"]

    built = json.loads(
        run(
            "task",
            "build",
            "cli-project",
            "--engine",
            "hybrid",
            "--instruction-file",
            str(instruction_file),
            "--output",
            str(output),
        ).stdout
    )
    task = json.loads(output.read_text(encoding="utf-8"))
    assert built["job_id"] == task["job_id"]
    assert task["task_text"] == instruction
    assert task["execution_engine"] == "hybrid"
    assert {"codex", "claude", "git", "tests"}.issubset(task["required_capabilities"])
