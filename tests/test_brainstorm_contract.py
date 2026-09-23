from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_controller.brainstorm_contract import (
    DEFAULT_CANDIDATE_COUNT,
    GENERAL_V1_RUBRIC,
    build_brainstorm_config,
    validate_brainstorm_config,
)
from hermes_controller.clock import FakeClock
from hermes_controller.controller import Controller, ControllerError
from hermes_controller.engine_policy import select_engine


REPO = Path(__file__).parents[1]
SCHEMAS = REPO / "hermes_controller" / "schemas"
BOTH_ENGINES_WORKER = {
    "worker_id": "main-linux", "platform": "linux", "environment": "WSL",
    "capabilities": ["codex", "claude", "git", "python", "tests"], "max_concurrent_jobs": 1,
}
CODEX_ONLY_WORKER = {
    "worker_id": "codex-only", "platform": "linux", "environment": "WSL",
    "capabilities": ["codex", "git", "python", "tests"], "max_concurrent_jobs": 1,
}


def manifest(root: Path, **overrides):
    spec = {
        "project_id": "sample-project",
        "repository": "git@example/sample.git",
        "ref": "main",
        "platform": "linux",
        "worker_id": "main-linux",
        "working_directory": str(root / "projects" / "sample-project"),
        "runtime_directory": str(root / "runtime" / "sample-project"),
        "allowed_engines": ["codex", "claude", "hybrid", "brainstorm"],
        "default_engine": "auto",
        "engine_policy": "balanced-v1",
        "capabilities": ["git", "python", "tests"],
        "task_type": "development",
        "execution_profile": "hermes",
        "human_gates": ["YOUTUBE_PUBLICATION_APPROVAL"],
        "idempotency_policy": "safe_retry",
        "timeout_seconds": 1800,
        "max_turns": 12,
    }
    spec.update(overrides)
    return spec


def custom_rubric():
    return [
        {"id": "impact", "label": "Impact", "description": "Expected value.", "weight": 50},
        {"id": "cost", "label": "Cost", "description": "Production cost.", "weight": 30},
        {"id": "risk", "label": "Risk", "description": "Execution risk.", "weight": 20},
    ]


@pytest.fixture
def controller(tmp_path):
    instance = Controller(tmp_path / "controller", clock=FakeClock(1_000))
    yield instance
    instance.close()


# --- brainstorm config -------------------------------------------------------

def test_default_config_uses_three_candidates_and_general_v1_rubric():
    config = build_brainstorm_config()
    assert DEFAULT_CANDIDATE_COUNT == 3
    assert config["version"] == "brainstorm-v1"
    assert config["candidate_count"] == 3
    assert config["rubric_id"] == "general-v1"
    assert config["rubric"] == [dict(item) for item in GENERAL_V1_RUBRIC]
    assert [item["id"] for item in config["rubric"]] == [
        "value", "originality", "project_fit", "feasibility", "repeatability", "evidence", "pilotability",
    ]
    assert sum(item["weight"] for item in config["rubric"]) == 100
    validate_brainstorm_config(config)


def test_default_config_is_an_independent_copy():
    config = build_brainstorm_config()
    config["rubric"][0]["weight"] = 99
    assert build_brainstorm_config()["rubric"][0]["weight"] == 25


@pytest.mark.parametrize("count", [2, 3, 4, 5, 6])
def test_candidate_count_accepts_two_to_six(count):
    assert build_brainstorm_config(candidate_count=count)["candidate_count"] == count


@pytest.mark.parametrize("count", [0, 1, 7, -3, True, "3", 3.0, None])
def test_candidate_count_rejects_out_of_range_or_non_integer(count):
    config = build_brainstorm_config()
    config["candidate_count"] = count
    with pytest.raises(ValueError, match="candidate_count"):
        validate_brainstorm_config(config)


def test_custom_rubric_is_materialized_as_custom():
    config = build_brainstorm_config(candidate_count=4, rubric=custom_rubric())
    assert config["rubric_id"] == "custom"
    assert config["rubric"] == custom_rubric()
    validate_brainstorm_config(config)


def test_empty_rubric_falls_back_to_general_v1():
    assert build_brainstorm_config(rubric=[])["rubric_id"] == "general-v1"


@pytest.mark.parametrize("mutate, message", [
    (lambda r: r[0].update(weight=40), "sum"),
    (lambda r: r[1].update(id="impact"), "unique"),
    (lambda r: r[0].update(id="Bad-ID"), "id"),
    (lambda r: r[0].update(weight=True), "weight"),
    (lambda r: r[0].update(weight=50.0), "weight"),
    (lambda r: r[0].update(weight=0), "weight"),
    (lambda r: r[0].update(label=""), "label"),
    (lambda r: r[0].update(label="x" * 81), "label"),
    (lambda r: r[0].update(description="x" * 501), "description"),
    (lambda r: r[0].update(extra="nope"), "fields"),
    (lambda r: r[0].pop("description"), "fields"),
    (lambda r: r.pop(), "3 and 10"),
])
def test_custom_rubric_validation(mutate, message):
    rubric = custom_rubric()
    mutate(rubric)
    with pytest.raises(ValueError, match=message):
        build_brainstorm_config(rubric=rubric)


def test_rubric_accepts_at_most_ten_criteria():
    rubric = [{"id": f"c{index}", "label": f"C{index}", "description": "d", "weight": 10} for index in range(10)]
    assert len(build_brainstorm_config(rubric=rubric)["rubric"]) == 10
    rubric.append({"id": "c10", "label": "C10", "description": "d", "weight": 0})
    with pytest.raises(ValueError, match="3 and 10"):
        build_brainstorm_config(rubric=rubric)


def test_config_rejects_tampered_general_v1_and_unknown_fields():
    tampered = build_brainstorm_config()
    tampered["rubric"][0]["weight"] = 20
    tampered["rubric"][1]["weight"] = 20
    with pytest.raises(ValueError, match="general-v1"):
        validate_brainstorm_config(tampered)
    for key, value in (("version", "brainstorm-v2"), ("rubric_id", "other")):
        bad = build_brainstorm_config()
        bad[key] = value
        with pytest.raises(ValueError):
            validate_brainstorm_config(bad)
    extra = build_brainstorm_config()
    extra["schema"] = {"type": "object"}
    with pytest.raises(ValueError, match="fields"):
        validate_brainstorm_config(extra)
    with pytest.raises(ValueError):
        validate_brainstorm_config([])


# --- balanced-v1 compatibility ------------------------------------------------

INSTRUCTIONS = [
    "Corrige el fallo del test unitario.",
    "Analiza la arquitectura y diseña una estrategia.",
    "Revisa la seguridad del cambio antes de producción.",
    "Haz una lluvia de ideas para un nuevo formato de vídeo.",
    "brainstorm new video formats",
    "Texto neutro sin señales.",
]
ALLOWED_SETS = [
    ["codex"], ["claude"], ["hybrid"], ["native"],
    ["codex", "claude"], ["claude", "hybrid"], ["codex", "hybrid"],
    ["codex", "claude", "hybrid"], ["codex", "claude", "hybrid", "native"],
]


@pytest.mark.parametrize("allowed", ALLOWED_SETS)
@pytest.mark.parametrize("instruction", INSTRUCTIONS)
def test_balanced_v1_ignores_brainstorm_and_keeps_existing_decisions(instruction, allowed):
    baseline = select_engine(instruction, allowed)
    for position in range(len(allowed) + 1):
        with_brainstorm = [*allowed[:position], "brainstorm", *allowed[position:]]
        assert select_engine(instruction, with_brainstorm) == baseline


def test_balanced_v1_never_selects_brainstorm_alone():
    with pytest.raises(ValueError, match="auto-selectable"):
        select_engine("Haz un brainstorm.", ["brainstorm"])


# --- project manifest ---------------------------------------------------------

def test_manifest_allows_brainstorm_as_opt_in_engine(controller, tmp_path):
    assert controller.register_project(manifest(tmp_path)) == "sample-project"


def test_manifest_rejects_brainstorm_as_default_engine(controller, tmp_path):
    with pytest.raises(ControllerError, match="explicit-only"):
        controller.register_project(manifest(tmp_path, default_engine="brainstorm"))


def test_manifest_auto_requires_an_auto_selectable_engine(controller, tmp_path):
    with pytest.raises(ControllerError, match="auto-selectable"):
        controller.register_project(manifest(tmp_path, allowed_engines=["brainstorm"]))
    controller.register_project(manifest(tmp_path, allowed_engines=["brainstorm", "codex"], default_engine="codex"))


# --- task builder ---------------------------------------------------------------

def test_builder_creates_explicit_brainstorm_snapshot(controller, tmp_path):
    controller.register_project(manifest(tmp_path))
    task = controller.build_project_task("sample-project", "¿Qué formato de vídeo probamos?", engine="brainstorm")
    assert task["execution_engine"] == "brainstorm"
    assert task["engine_selection"] == {"mode": "explicit", "selected_engine": "brainstorm"}
    assert task["task_type"] == "brainstorm"
    assert task["execution_profile"] == "brainstorm"
    assert task["human_gates"] == []
    assert task["idempotency_policy"] == "safe_retry"
    assert {"claude", "codex"}.issubset(task["required_capabilities"])
    assert task["brainstorm"] == build_brainstorm_config()
    assert task["timeout_seconds"] == 1800


def test_builder_accepts_candidate_count_and_custom_rubric(controller, tmp_path):
    controller.register_project(manifest(tmp_path))
    task = controller.build_project_task(
        "sample-project", "Idea", engine="brainstorm",
        brainstorm_candidates=5, brainstorm_rubric=custom_rubric(),
    )
    assert task["brainstorm"]["candidate_count"] == 5
    assert task["brainstorm"]["rubric_id"] == "custom"


def test_builder_rejects_invalid_brainstorm_options(controller, tmp_path):
    controller.register_project(manifest(tmp_path))
    with pytest.raises(ControllerError, match="candidate_count"):
        controller.build_project_task("sample-project", "Idea", engine="brainstorm", brainstorm_candidates=7)
    with pytest.raises(ControllerError, match="brainstorm options"):
        controller.build_project_task("sample-project", "Idea", engine="codex", brainstorm_candidates=3)
    with pytest.raises(ControllerError, match="brainstorm options"):
        controller.build_project_task("sample-project", "Idea", brainstorm_rubric=custom_rubric())


def test_builder_requires_project_opt_in(controller, tmp_path):
    controller.register_project(manifest(tmp_path, allowed_engines=["codex", "claude", "hybrid"]))
    with pytest.raises(ControllerError, match="not allowed"):
        controller.build_project_task("sample-project", "Idea", engine="brainstorm")


@pytest.mark.parametrize("instruction", INSTRUCTIONS)
def test_builder_auto_never_selects_brainstorm(controller, tmp_path, instruction):
    controller.register_project(manifest(tmp_path))
    task = controller.build_project_task("sample-project", instruction)
    assert task["execution_engine"] != "brainstorm"
    assert "brainstorm" not in task
    assert task["task_type"] == "development"


# --- task validation, queue and envelope ---------------------------------------

def brainstorm_task(controller, tmp_path):
    controller.register_project(manifest(tmp_path))
    return controller.build_project_task("sample-project", "Idea", engine="brainstorm")


@pytest.mark.parametrize("mutate, message", [
    (lambda t: t.update(task_type="development"), "brainstorm task_type"),
    (lambda t: t.update(execution_profile="hermes"), "brainstorm execution_profile"),
    (lambda t: t.pop("brainstorm"), "brainstorm config"),
    (lambda t: t["brainstorm"].update(candidate_count=9), "candidate_count"),
    (lambda t: t.update(required_capabilities=["codex", "git"]), "capabilities"),
])
def test_enqueue_rejects_inconsistent_brainstorm_task(controller, tmp_path, mutate, message):
    task = brainstorm_task(controller, tmp_path)
    mutate(task)
    with pytest.raises(ControllerError, match=message):
        controller.enqueue(task)


def test_enqueue_rejects_brainstorm_fields_on_other_engines(controller, tmp_path):
    controller.register_project(manifest(tmp_path))
    task = controller.build_project_task("sample-project", "Corrige el test.", engine="codex")
    with_block = copy.deepcopy(task)
    with_block["brainstorm"] = build_brainstorm_config()
    with pytest.raises(ControllerError, match="brainstorm config"):
        controller.enqueue(with_block)
    with_type = copy.deepcopy(task)
    with_type["task_type"] = "brainstorm"
    with pytest.raises(ControllerError, match="brainstorm task_type"):
        controller.enqueue(with_type)


def test_brainstorm_job_is_claimed_only_by_worker_with_both_engines_and_ingested(controller, tmp_path):
    task = brainstorm_task(controller, tmp_path)
    controller.register_worker(CODEX_ONLY_WORKER)
    controller.register_worker(BOTH_ENGINES_WORKER)
    job_id = controller.enqueue(task)
    assert controller.claim("codex-only") is None
    claim = controller.claim("main-linux")
    assert claim["job_id"] == job_id
    controller.ingest_result({
        "job_id": claim["job_id"], "run_id": claim["run_id"], "attempt": claim["attempt"],
        "worker_id": "main-linux", "lease_id": claim["lease_id"], "lease_token": claim["lease_token"],
        "started_at": 1_000, "finished_at": 1_001, "engine": "brainstorm",
        "engine_result": {"status": "DONE", "summary": "report ready", "gate": None,
                          "completed": [], "remaining": [], "evidence": []},
        "artifacts": [], "hashes": {},
    })
    status = controller.status(job_id)
    assert status["state"] == "DONE"
    assert status["engine"] == "brainstorm"


# --- published JSON schemas ------------------------------------------------------

def test_published_schemas_declare_brainstorm():
    task_schema = json.loads((SCHEMAS / "hermes-task.schema.json").read_text(encoding="utf-8"))
    assert "brainstorm" in task_schema["properties"]["execution_engine"]["enum"]
    assert "brainstorm" in task_schema["properties"]["engine_selection"]["properties"]["selected_engine"]["enum"]
    assert task_schema["properties"]["brainstorm"]["properties"]["candidate_count"] == {
        "type": "integer", "minimum": 2, "maximum": 6,
    }
    envelope_schema = json.loads((SCHEMAS / "hermes-controller-result-envelope.schema.json").read_text(encoding="utf-8"))
    assert "brainstorm" in envelope_schema["properties"]["engine"]["enum"]
    project_schema = json.loads((SCHEMAS / "hermes-project.schema.json").read_text(encoding="utf-8"))
    assert "brainstorm" in project_schema["properties"]["allowed_engines"]["items"]["enum"]
    assert "brainstorm" not in project_schema["properties"]["default_engine"]["enum"]


# --- CLI -----------------------------------------------------------------------------

def test_cli_builds_explicit_brainstorm_task(tmp_path):
    runtime = tmp_path / "controller"
    manifest_file = tmp_path / "project.json"
    manifest_file.write_text(json.dumps(manifest(tmp_path)), encoding="utf-8")
    rubric_file = tmp_path / "rubric.json"
    rubric_file.write_text(json.dumps(custom_rubric()), encoding="utf-8")

    def run(*args, check=True):
        return subprocess.run(
            [sys.executable, "-m", "hermes_controller", "--runtime-root", str(runtime), *args],
            cwd=REPO, text=True, capture_output=True, check=check,
        )

    run("project", "register", str(manifest_file))
    built = json.loads(run(
        "task", "build", "sample-project", "--engine", "brainstorm",
        "--candidates", "4", "--rubric-file", str(rubric_file), "--instruction", "Idea",
    ).stdout)
    assert built["execution_engine"] == "brainstorm"
    assert built["brainstorm"]["candidate_count"] == 4
    assert built["brainstorm"]["rubric"] == custom_rubric()

    default = json.loads(run("task", "build", "sample-project", "--engine", "brainstorm", "--instruction", "Idea").stdout)
    assert default["brainstorm"]["candidate_count"] == 3

    rejected = run("task", "build", "sample-project", "--engine", "codex", "--candidates", "3",
                   "--instruction", "Idea", check=False)
    assert rejected.returncode != 0
    assert "brainstorm options" in rejected.stderr
