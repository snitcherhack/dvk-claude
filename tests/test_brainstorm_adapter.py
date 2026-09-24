from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from hermes_controller.adapters import StructuredExecutionError, StructuredExecutionResult
from hermes_controller.artifacts import MAX_INLINE_TEXT_BYTES, validate_artifacts
from hermes_controller.brainstorm import (
    CHECKPOINT_SCHEMA_VERSION,
    STAGE_TIMEOUT_CAP_SECONDS,
    BrainstormAdapter,
    render_markdown,
)
from hermes_controller.brainstorm_contract import build_brainstorm_config

RUBRIC = [
    {"id": "appeal", "label": "Appeal", "description": "Attractiveness.", "weight": 50},
    {"id": "cost", "label": "Cost", "description": "Low cost.", "weight": 30},
    {"id": "clarity", "label": "Clarity", "description": "Clarity.", "weight": 20},
]
QUESTION = "Which weekend format should the coffee shop pilot? Constraint: budget under 100 EUR."
ENGINE_TAG = {"codex": "K", "claude": "L"}
MINIMUM_ARTIFACTS = {
    "brainstorm-input.json", "codex-proposals.json", "claude-proposals.json", "candidates-anonymized.json",
    "claude-evaluation.json", "codex-evaluation.json", "ranking.json", "winner-refinement.json",
    "winner-validation.json", "brainstorm-report.json", "brainstorm-report.md",
}


class Clock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeEngine:
    """Structured engine double that answers from the stage's real inputs."""

    def __init__(self, name: str, log: list, clock: Clock) -> None:
        self.name, self.log, self.clock = name, log, clock
        self.favourite = "Idea K0"
        self.verdict = "PASS"
        self.script: dict[str, list] = {}   # stage -> queued outcomes (payload/Exception/"invalid")
        self.hook = None
        self.seconds_per_call = 0.0
        self.proposal_size = 1

    def execute_structured(self, task, *, stage_schema, stage_roots, timeout_seconds):
        request_dir = Path(task["brain"]["task_file"]).parent
        exec_dir = Path(task["run_output_dir"])
        self.log.append({"engine": self.name, "stage": stage_schema, "task": json.loads(json.dumps(task)),
                         "roots": list(stage_roots), "timeout": timeout_seconds, "request_dir": request_dir,
                         "exec_dir": exec_dir, "stage_root": request_dir.parent,
                         "visible_before": sorted(p.name for p in exec_dir.iterdir())})
        self.clock.now += self.seconds_per_call
        (exec_dir / "fake.log").write_text(f"{self.name} {stage_schema}\n", encoding="utf-8")
        if self.hook:
            self.hook(self.name, stage_schema, exec_dir)
        queued = self.script.get(stage_schema)
        if queued:
            outcome = queued.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            if outcome == "invalid":
                (exec_dir / "invalid-output.json").write_text('{"unexpected": true}', encoding="utf-8")
                return StructuredExecutionResult(stage=stage_schema, payload={"unexpected": True})
        payload = getattr(self, f"_{stage_schema}")(request_dir)
        return StructuredExecutionResult(stage=stage_schema, payload=payload, evidence=[str(exec_dir / "fake.log")])

    def _proposals(self, stage_dir: Path) -> dict:
        text = (stage_dir / "task.md").read_text(encoding="utf-8")
        count = int(text.split("Return exactly ")[1].split(" ")[0])
        tag = ENGINE_TAG[self.name]
        pad = "x" * self.proposal_size
        return {"proposals": [{
            "title": f"Idea {tag}{index}", "concept": f"Concept {tag}{index} {pad}",
            "hook": "Hook", "audience_flow": f"Flow {pad[:1400]}", "execution_plan": f"Plan {pad}", "dependencies": ["dep"],
            "assumptions": ["assumption"], "risks": [f"risk {tag}{index}"], "minimum_pilot": "One weekend.",
        } for index in range(count)]}

    def _evaluation(self, stage_dir: Path) -> dict:
        bundle = json.loads((stage_dir / "input" / "candidates-anonymized.json").read_text(encoding="utf-8"))
        return {"evaluations": [{
            "candidate_id": item["candidate_id"],
            "scores": [{"criterion_id": c["id"], "score": 9 if item["title"] == self.favourite else 5} for c in RUBRIC],
            "strengths": ["clear"], "weaknesses": [f"weak point {ENGINE_TAG[self.name]}"], "improvements": ["shorter"],
            "constraint_violations": [],
        } for item in bundle]}

    def _refinement(self, stage_dir: Path) -> dict:
        winner = json.loads((stage_dir / "input" / "winner.json").read_text(encoding="utf-8"))
        return {"candidate_id": winner["candidate_id"], "title": f"Refined {winner['title']}", "concept": "Refined.",
                "decisions_adopted": ["keep"], "discarded_elements": ["drop"], "accepted_risks": ["rain"],
                "pilot_definition": "One weekend.", "success_criterion": "20 visitors."}

    def _validation(self, stage_dir: Path) -> dict:
        refined = json.loads((stage_dir / "input" / "refined.json").read_text(encoding="utf-8"))
        return {"candidate_id": refined["candidate_id"], "verdict": self.verdict, "material_findings": ["finding"],
                "scores": [{"criterion_id": c["id"], "score": 8} for c in RUBRIC]}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
                          capture_output=True, text=True, check=True).stdout


@pytest.fixture
def world(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "README.md").write_text("coffee shop\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "init")
    run_output = tmp_path / "runtime" / "job-1"
    run_output.mkdir(parents=True)
    (run_output / "hermes-task.md").write_text(QUESTION, encoding="utf-8")
    clock = Clock()
    log: list = []
    claude, codex = FakeEngine("claude", log, clock), FakeEngine("codex", log, clock)
    return {"tmp": tmp_path, "repo": repo, "run": run_output, "clock": clock, "log": log,
            "claude": claude, "codex": codex}


def make_task(w, *, run_id="run-1", attempt=1, job_id="job-1", timeout=1800, **overrides):
    task = {
        "job_id": job_id, "project": "cafe", "task_type": "brainstorm", "execution_profile": "brainstorm",
        "execution_engine": "brainstorm", "working_directory": str(w["repo"]), "run_output_dir": str(w["run"]),
        "brain": {"repository": "inline://x", "ref": "cafe", "commit": "c", "task_file": str(w["run"] / "hermes-task.md")},
        "timeout_seconds": timeout, "human_gates": [], "idempotency_policy": "safe_retry",
        "brainstorm": build_brainstorm_config(candidate_count=2, rubric=RUBRIC),
        "_hermes_runtime": {"job_id": job_id, "run_id": run_id, "attempt": attempt},
    }
    task.update(overrides)
    return task


def run(w, **kwargs):
    adapter = BrainstormAdapter(w["claude"], w["codex"], clock=w["clock"])
    return adapter.execute(make_task(w, **kwargs))


def calls(w, since: int = 0) -> list[tuple[str, str]]:
    return [(item["engine"], item["stage"]) for item in w["log"][since:]]


def orchestration(w) -> Path:
    return w["run"] / "orchestration"


FULL_ORDER = [("codex", "proposals"), ("claude", "proposals"), ("claude", "evaluation"), ("codex", "evaluation"),
              ("codex", "refinement"), ("claude", "validation")]


# --- happy path -----------------------------------------------------------------------------------------

def test_done_recommended_runs_the_ten_stage_workflow(world):
    result = run(world)
    assert result.status == "DONE" and result.gate is None
    assert calls(world) == FULL_ORDER
    assert "RECOMMENDED_FOR_PILOT" in result.summary
    report = json.loads((orchestration(world) / "brainstorm-report.json").read_text(encoding="utf-8"))
    assert report["decision_status"] == "RECOMMENDED_FOR_PILOT"
    assert report["winner"]["title"] == "Idea K0"
    assert report["winner"]["candidate_id"] in result.summary
    assert report["confidence"] in result.summary
    assert result.remaining and "new" in result.remaining[0].lower()
    assert result.completed == ["prepare", "codex-proposals", "claude-proposals", "normalize", "claude-evaluation",
                                "codex-evaluation", "rank", "codex-refinement", "claude-validation", "report"]
    assert result.result()["status"] == "DONE"


def test_done_inconclusive_when_validation_fails(world):
    world["claude"].verdict = "FAIL"
    result = run(world)
    assert result.status == "DONE" and result.gate is None
    assert "INCONCLUSIVE" in result.summary
    report = json.loads((orchestration(world) / "brainstorm-report.json").read_text(encoding="utf-8"))
    assert report["decision_status"] == "INCONCLUSIVE"
    assert report["winner"]["title"] == "Idea K0" and report["runner_up"] is not None
    assert calls(world) == FULL_ORDER


def test_refiner_and_validator_follow_the_winner_author(world):
    world["claude"].favourite = world["codex"].favourite = "Idea L1"
    result = run(world)
    assert result.status == "DONE"
    assert calls(world)[-2:] == [("claude", "refinement"), ("codex", "validation")]


# --- layout, permissions and preparation ---------------------------------------------------------------------

def test_layout_and_permissions_do_not_depend_on_umask(world):
    previous = os.umask(0)
    try:
        run(world)
    finally:
        os.umask(previous)
    stage_roots = list((world["run"] / "stages").iterdir())
    for directory in [world["run"], orchestration(world), orchestration(world) / "checkpoints", world["run"] / "stages",
                      *stage_roots, *[p / "request" for p in stage_roots], *[p / "request" / "input" for p in stage_roots],
                      *[p / "executions" for p in stage_roots], *[c for p in stage_roots for c in (p / "executions").iterdir()]]:
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700, directory
    for stage_root in stage_roots:
        assert sorted(p.name for p in stage_root.iterdir()) == ["executions", "request"]
    hermes_files = [*orchestration(world).rglob("*"), *(world["run"] / "stages").glob("*/request/task.md"),
                    *(world["run"] / "stages").glob("*/request/input/*")]
    for path in hermes_files:
        if path.is_file():
            assert stat.S_IMODE(path.stat().st_mode) == 0o600, path
    assert MINIMUM_ARTIFACTS <= {p.name for p in orchestration(world).iterdir()}


def test_run_output_inside_repo_is_blocked(world):
    inside = world["repo"] / "runtime"
    inside.mkdir()
    (inside / "hermes-task.md").write_text(QUESTION, encoding="utf-8")
    result = run(world, run_output_dir=str(inside),
                 brain={"repository": "i", "ref": "r", "commit": "c", "task_file": str(inside / "hermes-task.md")})
    assert result.status == "BLOCKED"
    assert calls(world) == []


@pytest.mark.parametrize("runtime", [None, {}, {"job_id": "job-1", "run_id": "r"},
                                     {"job_id": "", "run_id": "r", "attempt": 1},
                                     {"job_id": "job-1", "run_id": "r", "attempt": 0},
                                     {"job_id": "job-1", "run_id": "r", "attempt": True},
                                     {"job_id": "job-1", "run_id": "r", "attempt": 1, "lease_token": "x"}])
def test_missing_or_invalid_runtime_context_is_blocked(world, runtime):
    task = make_task(world)
    if runtime is None:
        task.pop("_hermes_runtime")
    else:
        task["_hermes_runtime"] = runtime
    result = BrainstormAdapter(world["claude"], world["codex"], clock=world["clock"]).execute(task)
    assert result.status == "BLOCKED"
    assert calls(world) == []


@pytest.mark.parametrize("overrides", [
    {"execution_profile": "hermes"}, {"task_type": "development"},
    {"brainstorm": {"version": "brainstorm-v1", "candidate_count": 9, "rubric_id": "custom", "rubric": RUBRIC}},
    {"timeout_seconds": 0},
])
def test_invalid_task_is_blocked_before_models(world, overrides):
    result = run(world, **overrides)
    assert result.status == "BLOCKED"
    assert calls(world) == []


# --- baseline and repository integrity ----------------------------------------------------------------------

def test_baseline_is_created_once_and_reused(world):
    world["claude"].script["evaluation"] = [StructuredExecutionError("FAILED", "evaluation", "boom")]
    assert run(world).status == "FAILED"
    baseline = orchestration(world) / "baseline.json"
    first = baseline.read_bytes()
    record = json.loads(first)
    assert set(record["fingerprint"]) >= {"head", "status_sha256", "diff_sha256", "untracked"}
    assert run(world, run_id="run-2", attempt=2).status == "DONE"
    assert baseline.read_bytes() == first


def test_repo_changed_before_resume_fails_without_calling_models(world):
    world["claude"].script["evaluation"] = [StructuredExecutionError("FAILED", "evaluation", "boom")]
    assert run(world).status == "FAILED"
    before = len(world["log"])
    (world["repo"] / "README.md").write_text("changed by someone\n", encoding="utf-8")
    result = run(world, run_id="run-2", attempt=2)
    assert result.status == "FAILED" and "fingerprint" in result.summary
    assert len(world["log"]) == before


def test_repo_changed_during_workflow_fails_at_the_end(world):
    def mutate(engine, stage, _dir):
        if stage == "validation":
            (world["repo"] / "new-file.txt").write_text("written during run\n", encoding="utf-8")
    world["claude"].hook = mutate
    result = run(world)
    assert result.status == "FAILED" and "fingerprint" in result.summary


def test_dirty_repo_is_allowed_and_preserved(world):
    (world["repo"] / "README.md").write_text("local edit\n", encoding="utf-8")
    (world["repo"] / "notes.txt").write_text("untracked\n", encoding="utf-8")
    status_before = git(world["repo"], "status", "--porcelain")
    result = run(world)
    assert result.status == "DONE"
    assert git(world["repo"], "status", "--porcelain") == status_before
    assert (world["repo"] / "README.md").read_text(encoding="utf-8") == "local edit\n"
    fingerprint = json.loads((orchestration(world) / "baseline.json").read_text())["fingerprint"]
    assert "notes.txt" in fingerprint["untracked"]


def test_corrupt_baseline_fails(world):
    world["claude"].script["evaluation"] = [StructuredExecutionError("FAILED", "evaluation", "boom")]
    run(world)
    (orchestration(world) / "baseline.json").write_text("{not json", encoding="utf-8")
    before = len(world["log"])
    result = run(world, run_id="run-2", attempt=2)
    assert result.status == "FAILED" and "baseline" in result.summary
    assert len(world["log"]) == before


# --- independence and inputs -----------------------------------------------------------------------------------

def test_codex_proposals_run_before_claude_and_stay_independent(world):
    run(world)
    proposals = [item for item in world["log"] if item["stage"] == "proposals"]
    assert [item["engine"] for item in proposals] == ["codex", "claude"]
    for item in proposals:
        request_dir = item["request_dir"]
        texts = [p.read_text(encoding="utf-8") for p in request_dir.rglob("*") if p.is_file()]
        other = "L" if item["engine"] == "codex" else "K"
        assert not any(f"Idea {other}" in text for text in texts)
        assert QUESTION in (request_dir / "task.md").read_text(encoding="utf-8")
        assert item["roots"] == [str(world["repo"]), str(request_dir), str(item["exec_dir"])]
        assert "_hermes_runtime" not in item["task"]


def test_stage_roots_are_minimal_and_include_selected_workspaces(world):
    workspace = world["tmp"] / "workspace"
    workspace.mkdir()
    run(world, selected_workspaces={"assets": str(workspace)})
    for item in world["log"]:
        assert item["roots"] == [str(world["repo"]), str(workspace), str(item["request_dir"]), str(item["exec_dir"])]
        assert str(world["run"]) not in item["roots"]
        assert str(orchestration(world)) not in item["roots"]
        assert str(item["stage_root"]) not in item["roots"]


def test_codex_hidden_paths_cover_orchestration_and_siblings(world):
    run(world)
    for item in world["log"]:
        if item["engine"] != "codex":
            continue
        hidden = set(item["task"]["brainstorm_probe"]["hidden_paths"])
        assert str(orchestration(world)) in hidden
        assert str(world["run"] / "hermes-task.md") in hidden
        siblings = {str(p) for p in (world["run"] / "stages").iterdir() if p != item["stage_root"]}
        assert siblings <= hidden
        assert not {str(item["stage_root"]), str(item["request_dir"]), str(item["exec_dir"])} & hidden


def test_both_evaluators_receive_the_same_bundle_without_authors(world):
    run(world)
    evaluations = [item for item in world["log"] if item["stage"] == "evaluation"]
    bundles = [(item["request_dir"] / "input" / "candidates-anonymized.json").read_bytes() for item in evaluations]
    assert bundles[0] == bundles[1]
    for item in evaluations:
        texts = " ".join(p.read_text(encoding="utf-8") for p in item["request_dir"].rglob("*") if p.is_file()).lower()
        assert "claude" not in texts and "codex" not in texts and "author" not in texts
        assert {p.name for p in (item["request_dir"] / "input").iterdir()} == {"candidates-anonymized.json", "rubric.json"}


def test_refinement_and_validation_inputs(world):
    run(world)
    refine = next(item for item in world["log"] if item["stage"] == "refinement")
    names = {p.name for p in (refine["request_dir"] / "input").iterdir()}
    assert names == {"winner.json", "critiques.json", "rubric.json"}
    critiques = json.loads((refine["request_dir"] / "input" / "critiques.json").read_text())
    assert len(critiques) == 2
    text = " ".join(p.read_text(encoding="utf-8") for p in (refine["request_dir"] / "input").iterdir()).lower()
    assert "author" not in text and "claude" not in text and "codex" not in text
    validate = next(item for item in world["log"] if item["stage"] == "validation")
    assert {p.name for p in (validate["request_dir"] / "input").iterdir()} == {"refined.json", "rubric.json"}


# --- retries ----------------------------------------------------------------------------------------------------------

def test_semantic_retry_once_then_success(world):
    world["codex"].script["proposals"] = ["invalid"]
    result = run(world)
    assert result.status == "DONE"
    assert calls(world)[:3] == [("codex", "proposals"), ("codex", "proposals"), ("claude", "proposals")]
    budget = json.loads((orchestration(world) / "budget.json").read_text())
    assert budget["retries_used"] == 1 and budget["stage_retries"] == {"codex-proposals": 1}


def test_invalid_output_after_retry_fails_without_fallback(world):
    world["codex"].script["proposals"] = ["invalid", "invalid"]
    result = run(world)
    assert result.status == "FAILED" and "invalid" in result.summary
    assert calls(world) == [("codex", "proposals"), ("codex", "proposals")]


def test_at_most_two_retries_per_attempt(world):
    world["codex"].script["proposals"] = ["invalid"]
    world["claude"].script["proposals"] = ["invalid"]
    world["claude"].script["evaluation"] = ["invalid"]
    result = run(world)
    assert result.status == "FAILED" and "retry budget" in result.summary
    assert calls(world).count(("claude", "evaluation")) == 1


@pytest.mark.parametrize("status", ["BLOCKED", "FAILED"])
def test_engine_errors_are_not_retried(world, status):
    world["claude"].script["proposals"] = [StructuredExecutionError(status, "proposals", "engine problem")]
    result = run(world)
    assert result.status == status
    assert calls(world) == [("codex", "proposals"), ("claude", "proposals")]


def test_no_single_model_fallback_when_one_engine_is_blocked(world):
    world["codex"].script["proposals"] = [StructuredExecutionError("BLOCKED", "proposals", "isolation probe failed")]
    result = run(world)
    assert result.status == "BLOCKED"
    assert calls(world) == [("codex", "proposals")]
    assert not (orchestration(world) / "brainstorm-report.json").exists()


# --- budget -------------------------------------------------------------------------------------------------------------

def test_effective_timeout_is_capped_and_uses_remaining_budget(world):
    world["codex"].seconds_per_call = world["claude"].seconds_per_call = 250
    result = run(world, timeout=1000)
    timeouts = [item["timeout"] for item in world["log"]]
    assert STAGE_TIMEOUT_CAP_SECONDS == 300
    assert timeouts[:4] == [300, 300, 300, 250]
    assert result.status == "FAILED" and "budget" in result.summary


def test_same_attempt_keeps_budget_and_retries(world):
    world["claude"].script["evaluation"] = [StructuredExecutionError("FAILED", "evaluation", "crash")]
    world["codex"].script["proposals"] = ["invalid"]
    run(world, timeout=1200)
    world["clock"].now += 1000
    before = len(world["log"])
    run(world, timeout=1200)
    assert world["log"][before]["timeout"] == 200
    budget = json.loads((orchestration(world) / "budget.json").read_text())
    assert budget["retries_used"] == 1 and budget["run_id"] == "run-1"


def test_new_attempt_resets_budget_but_reuses_checkpoints(world):
    world["claude"].script["evaluation"] = [StructuredExecutionError("FAILED", "evaluation", "crash")]
    run(world, timeout=1200)
    world["clock"].now += 1000
    before = len(world["log"])
    result = run(world, timeout=1200, run_id="run-2", attempt=2)
    assert result.status == "DONE"
    assert calls(world, before) == FULL_ORDER[2:]
    assert world["log"][before]["timeout"] == 300
    budget = json.loads((orchestration(world) / "budget.json").read_text())
    assert (budget["run_id"], budget["attempt"], budget["retries_used"]) == ("run-2", 2, 0)


# --- checkpoints -------------------------------------------------------------------------------------------------------

def test_completed_run_is_fully_reused_from_checkpoints(world):
    first = run(world)
    before = len(world["log"])
    second = run(world, run_id="run-2", attempt=2)
    assert calls(world, before) == []
    assert second.status == "DONE" and second.summary == first.summary
    assert second.hashes == first.hashes


def test_checkpoint_format(world):
    run(world)
    checkpoint = json.loads((orchestration(world) / "checkpoints" / "codex-proposals.json").read_text())
    assert checkpoint["schema_version"] == CHECKPOINT_SCHEMA_VERSION
    assert checkpoint["engine"] == "codex" and checkpoint["stage"] == "proposals"
    output = orchestration(world) / "codex-proposals.json"
    assert checkpoint["output_sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert len(checkpoint["input_sha256"]) == 64


@pytest.mark.parametrize("tamper", ["schema_version", "input_sha256", "output_sha256", "output_file"])
def test_mismatched_checkpoint_forces_recalculation(world, tamper):
    run(world)
    checkpoint_path = orchestration(world) / "checkpoints" / "claude-proposals.json"
    if tamper == "output_file":
        (orchestration(world) / "claude-proposals.json").write_text('{"proposals": []}', encoding="utf-8")
    else:
        checkpoint = json.loads(checkpoint_path.read_text())
        checkpoint[tamper] = "0" * 64 if tamper != "schema_version" else "old"
        checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
    before = len(world["log"])
    assert run(world, run_id="run-2", attempt=2).status == "DONE"
    assert ("claude", "proposals") in calls(world, before)
    assert ("codex", "proposals") not in calls(world, before)


def test_corrupt_checkpoint_fails(world):
    run(world)
    (orchestration(world) / "checkpoints" / "codex-proposals.json").write_text("{broken", encoding="utf-8")
    result = run(world, run_id="run-2", attempt=2)
    assert result.status == "FAILED" and "checkpoint" in result.summary


def test_runtime_context_does_not_change_input_hashes(world):
    run(world)
    first = {p.name: json.loads(p.read_text())["input_sha256"] for p in (orchestration(world) / "checkpoints").iterdir()}
    before = len(world["log"])
    run(world, run_id="another-run", attempt=7)
    second = {p.name: json.loads(p.read_text())["input_sha256"] for p in (orchestration(world) / "checkpoints").iterdir()}
    assert first == second
    assert calls(world, before) == []


def test_changed_rubric_invalidates_checkpoints(world):
    run(world)
    before = len(world["log"])
    rubric = [dict(item) for item in RUBRIC]
    rubric[0]["description"] = "Changed."
    result = run(world, run_id="run-2", attempt=2, brainstorm=build_brainstorm_config(candidate_count=2, rubric=rubric))
    assert result.status == "DONE"
    assert calls(world, before) == FULL_ORDER


# --- artifacts and report --------------------------------------------------------------------------------------------

def test_artifacts_come_from_real_bytes_and_ignore_sandbox_dirs(world):
    def plant(engine, _stage, stage_dir):
        if engine == "codex":
            for name in (".agents", ".codex", ".git"):
                (stage_dir / name).mkdir(exist_ok=True)
    world["codex"].hook = plant
    result = run(world)
    validate_artifacts(result.artifacts, result.hashes)
    paths = {item["path"] for item in result.artifacts}
    assert paths == {f"orchestration/{name}" for name in MINIMUM_ARTIFACTS}
    for item in result.artifacts:
        data = (world["run"] / item["path"]).read_bytes()
        assert item["bytes"] == len(data) and item["sha256"] == hashlib.sha256(data).hexdigest()
        assert result.hashes[item["path"]] == item["sha256"]
    inline = [item for item in result.artifacts if "inline_text" in item]
    assert [item["path"] for item in inline] == ["orchestration/brainstorm-report.md"]


def test_markdown_report_content(world):
    run(world)
    markdown = (orchestration(world) / "brainstorm-report.md").read_text(encoding="utf-8")
    for heading in ("Question", "Decision", "Ranking", "Winner", "Confidence", "Self-preference", "Divergences",
                    "Refinement", "Validation", "Risks"):
        assert f"## {heading}" in markdown
    assert QUESTION in markdown
    assert "not a consensus" in markdown
    assert "RECOMMENDED_FOR_PILOT" in markdown
    report = json.loads((orchestration(world) / "brainstorm-report.json").read_text(encoding="utf-8"))
    assert render_markdown(report) == markdown


def test_large_report_uses_separate_inline_summary(world):
    world["claude"].proposal_size = world["codex"].proposal_size = 1900
    result = run(world, brainstorm=build_brainstorm_config(candidate_count=6, rubric=RUBRIC))
    assert result.status == "DONE"
    main = world["run"] / "orchestration" / "brainstorm-report.md"
    assert main.stat().st_size > MAX_INLINE_TEXT_BYTES
    by_path = {item["path"]: item for item in result.artifacts}
    assert "inline_text" not in by_path["orchestration/brainstorm-report.md"]
    inline = by_path["orchestration/brainstorm-report.inline.md"]
    data = (world["run"] / inline["path"]).read_bytes()
    assert len(data) <= MAX_INLINE_TEXT_BYTES
    assert inline["inline_text"].encode("utf-8") == data
    validate_artifacts(result.artifacts, result.hashes)


def test_failed_result_exposes_only_validated_outputs_without_inline(world):
    world["claude"].script["evaluation"] = [StructuredExecutionError("FAILED", "evaluation", "crash")]
    result = run(world)
    assert result.status == "FAILED"
    paths = {item["path"] for item in result.artifacts}
    assert paths == {"orchestration/brainstorm-input.json", "orchestration/codex-proposals.json",
                     "orchestration/claude-proposals.json", "orchestration/candidates-anonymized.json"}
    assert not any("inline_text" in item for item in result.artifacts)
    validate_artifacts(result.artifacts, result.hashes)


def test_planted_symlink_in_orchestration_is_replaced_not_followed(world):
    world["claude"].script["evaluation"] = [StructuredExecutionError("FAILED", "evaluation", "crash")]
    run(world)
    target = world["tmp"] / "outside.txt"
    target.write_text("untouched", encoding="utf-8")
    ranking = orchestration(world) / "ranking.json"
    ranking.symlink_to(target)
    assert run(world, run_id="run-2", attempt=2).status == "DONE"
    assert target.read_text(encoding="utf-8") == "untouched"
    assert not ranking.is_symlink()


def test_never_returns_wait_user(world):
    world["claude"].verdict = "FAIL"
    statuses = {run(world).status}
    assert "WAIT_USER" not in statuses


# --- request (read-only) vs execution (read-write) separation ------------------------------------------------

def test_codex_writable_root_is_a_fresh_execution_root_not_the_request(world):
    run(world)
    for item in world["log"]:
        output = Path(item["task"]["run_output_dir"])
        assert output == item["exec_dir"] and output.parent.name == "executions"
        assert output != item["request_dir"]
        assert item["request_dir"] not in output.parents and output not in item["request_dir"].parents
        assert item["roots"][-1] == str(output) and str(item["request_dir"]) in item["roots"]


def test_task_and_inputs_are_not_under_the_execution_root(world):
    run(world)
    for item in world["log"]:
        task_file = Path(item["task"]["brain"]["task_file"])
        assert task_file == item["request_dir"] / "task.md"
        assert item["exec_dir"] not in task_file.parents
        assert item["exec_dir"] not in (item["request_dir"] / "input").parents


def test_writes_in_the_execution_root_cannot_touch_the_request(world):
    def tamper(_engine, _stage, exec_dir):
        (exec_dir / "task.md").write_text("tampered", encoding="utf-8")
        (exec_dir / "input").mkdir()
        (exec_dir / "input" / "candidates-anonymized.json").write_text("[]", encoding="utf-8")
    world["codex"].hook = tamper
    assert run(world).status == "DONE"
    for item in world["log"]:
        assert "tampered" not in (item["request_dir"] / "task.md").read_text(encoding="utf-8")
    evaluation = next(item for item in world["log"] if item["engine"] == "codex" and item["stage"] == "evaluation")
    assert json.loads((evaluation["request_dir"] / "input" / "candidates-anonymized.json").read_text()) != []


def test_semantic_retry_gets_a_new_execution_root_without_previous_files(world):
    world["codex"].script["proposals"] = ["invalid"]
    assert run(world).status == "DONE"
    first, second = [item for item in world["log"] if item["engine"] == "codex" and item["stage"] == "proposals"]
    assert first["exec_dir"] != second["exec_dir"]
    assert (first["exec_dir"].name, second["exec_dir"].name) == ("call-0001", "call-0002")
    assert second["visible_before"] == []
    assert str(first["exec_dir"]) not in second["roots"]
    assert str(first["exec_dir"]) in second["task"]["brainstorm_probe"]["hidden_paths"]
    assert (first["exec_dir"] / "invalid-output.json").is_file()   # kept as internal evidence, never exposed


def test_new_controller_attempt_without_checkpoint_uses_a_new_execution_root(world):
    world["claude"].script["evaluation"] = [StructuredExecutionError("FAILED", "evaluation", "crash")]
    run(world)
    run(world, run_id="run-2", attempt=2)
    roots = [item["exec_dir"].name for item in world["log"] if item["engine"] == "claude" and item["stage"] == "evaluation"]
    assert roots == ["call-0001", "call-0002"]


def test_checkpoint_reuse_creates_no_call_and_no_execution_root(world):
    run(world)
    before = sorted(str(p) for p in (world["run"] / "stages").glob("*/executions/*"))
    calls_before = len(world["log"])
    run(world, run_id="run-2", attempt=2)
    assert len(world["log"]) == calls_before
    assert sorted(str(p) for p in (world["run"] / "stages").glob("*/executions/*")) == before


def test_input_sha256_does_not_depend_on_the_execution_root(world):
    run(world)
    checkpoint = orchestration(world) / "checkpoints" / "codex-proposals.json"
    original = json.loads(checkpoint.read_text())["input_sha256"]
    checkpoint.unlink()
    before = len(world["log"])
    assert run(world, run_id="run-2", attempt=2).status == "DONE"
    assert calls(world, before) == [("codex", "proposals")]
    assert world["log"][-1]["exec_dir"].name == "call-0002"
    assert json.loads(checkpoint.read_text())["input_sha256"] == original


def test_previous_execution_outputs_never_become_artifacts(world):
    world["codex"].script["proposals"] = ["invalid"]
    result = run(world)
    assert all(item["path"].startswith("orchestration/") for item in result.artifacts)
    assert not any("executions" in path or "invalid-output" in path for path in result.hashes)


@pytest.mark.parametrize("planted", ["request", "request/input", "executions", "executions/call-0001"])
def test_symlinks_in_request_or_execution_layout_fail_closed(world, planted):
    stage_root = world["run"] / "stages" / "codex-proposals"
    target = world["tmp"] / "elsewhere"
    target.mkdir()
    link = stage_root / planted
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target, target_is_directory=True)
    result = run(world)
    assert result.status == "FAILED"
    assert calls(world) == []
    assert list(target.iterdir()) == []
    assert link.is_symlink()
