"""Phase 8: local end-to-end Brainstorm through the real Controller, HTTP API and WorkerDaemon.

Only the two base structured engines are fakes (no network, no subprocess).
Everything else is the production path: project registry, task builder,
queue, HTTP enrol/claim/heartbeat/ingest, worker multi-engine construction,
EngineRoutingAdapter, BrainstormAdapter, artifacts and Controller.status().
"""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import threading
import time
from pathlib import Path

import pytest

import hermes_controller.worker as worker_module
from hermes_controller.adapters import (
    AdapterResult,
    EngineRoutingAdapter,
    HybridAdapter,
    StructuredExecutionError,
    StructuredExecutionResult,
)
from hermes_controller.api import serve
from hermes_controller.artifacts import validate_artifacts
from hermes_controller.brainstorm import BrainstormAdapter, repo_fingerprint
from hermes_controller.clock import FakeClock
from hermes_controller.controller import LEASE_DURATION_MS, Controller, ControllerError
from hermes_controller.worker import TransportError, WorkerDaemon

QUESTION = "Propose three ways to improve the developer onboarding documentation for this test repository."
WORKER_ID = "main-linux-test"
WORKER_TOKEN = "integration-worker-token"
TAG = {"codex": "K", "claude": "L"}
MINIMUM_ARTIFACTS = [
    "brainstorm-input.json", "codex-proposals.json", "claude-proposals.json", "candidates-anonymized.json",
    "claude-evaluation.json", "codex-evaluation.json", "ranking.json", "winner-refinement.json",
    "winner-validation.json", "brainstorm-report.json", "brainstorm-report.md",
]


# --- fake structured engines ---------------------------------------------------------------------------------

class Fleet:
    """Shared state for the fake engines a worker builds (survives worker restarts)."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.scripts: dict[tuple[str, str], list] = {}
        self.hooks: dict[tuple[str, str], object] = {}
        self.verdict = "PASS"
        self.brainstorm_tasks: list[dict] = []

    def counts(self, since: int = 0) -> dict[str, int]:
        result: dict[str, int] = {}
        for call in self.calls[since:]:
            key = f"{call['engine']}-{call['stage']}"
            result[key] = result.get(key, 0) + 1
        return result


class FakeStructuredEngine:
    """Deterministic stand-in for ClaudeAgentAdapter/CodexRunAdapter.execute_structured."""

    def __init__(self, name: str, fleet: Fleet) -> None:
        self.name, self.fleet = name, fleet

    def execute(self, task):  # base engines are only used structurally in these tests
        return AdapterResult(status="BLOCKED", summary=f"fake {self.name} has no free-form execution")

    def execute_structured(self, task, *, stage_schema, stage_roots, timeout_seconds):
        request = Path(task["brain"]["task_file"]).parent
        execution = Path(task["run_output_dir"])
        self.fleet.calls.append({"engine": self.name, "stage": stage_schema, "task": copy.deepcopy(task),
                                 "roots": list(stage_roots), "timeout": timeout_seconds,
                                 "call": len(self.fleet.calls) + 1, "execution": execution.name})
        (execution / "fake-engine.log").write_text(f"{self.name} {stage_schema}\n", encoding="utf-8")
        hook = self.fleet.hooks.get((self.name, stage_schema))
        if hook:
            hook()
        queued = self.fleet.scripts.get((self.name, stage_schema))
        if queued:
            outcome = queued.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            if outcome == "invalid":
                return StructuredExecutionResult(stage=stage_schema, payload={"unexpected": True})
        payload = getattr(self, f"_{stage_schema}")(request)
        return StructuredExecutionResult(stage=stage_schema, payload=payload,
                                         evidence=[str(execution / "fake-engine.log")])

    def _proposals(self, request: Path) -> dict:
        text = (request / "task.md").read_text(encoding="utf-8")
        count = int(text.split("Return exactly ")[1].split(" ")[0])
        tag = TAG[self.name]
        return {"proposals": [{
            "title": f"Idea {tag}{index}", "concept": f"Onboarding improvement {tag}{index}.",
            "hook": "Faster first day", "audience_flow": "New developer reads the guide.",
            "execution_plan": "Rewrite the README section.", "dependencies": ["maintainer review"],
            "assumptions": ["docs are read"], "risks": [f"stale docs {tag}{index}"], "minimum_pilot": "One new hire.",
        } for index in range(count)]}

    def _evaluation(self, request: Path) -> dict:
        bundle = json.loads((request / "input" / "candidates-anonymized.json").read_text(encoding="utf-8"))
        rubric = json.loads((request / "input" / "rubric.json").read_text(encoding="utf-8"))
        return {"evaluations": [{
            "candidate_id": item["candidate_id"],
            "scores": [{"criterion_id": c["id"], "score": 9 if item["title"] == "Idea K0" else 5} for c in rubric],
            "strengths": ["clear"], "weaknesses": ["needs examples"], "improvements": ["add a checklist"],
            "constraint_violations": [],
        } for item in bundle]}

    def _refinement(self, request: Path) -> dict:
        winner = json.loads((request / "input" / "winner.json").read_text(encoding="utf-8"))
        return {"candidate_id": winner["candidate_id"], "title": f"Refined {winner['title']}", "concept": "Refined.",
                "decisions_adopted": ["checklist"], "discarded_elements": ["video"], "accepted_risks": ["drift"],
                "pilot_definition": "Next new hire.", "success_criterion": "Setup under one hour."}

    def _validation(self, request: Path) -> dict:
        refined = json.loads((request / "input" / "refined.json").read_text(encoding="utf-8"))
        rubric = json.loads((request / "input" / "rubric.json").read_text(encoding="utf-8"))
        return {"candidate_id": refined["candidate_id"], "verdict": self.fleet.verdict,
                "material_findings": ["check links"], "scores": [{"criterion_id": c["id"], "score": 8} for c in rubric]}


# --- environment -------------------------------------------------------------------------------------------------

def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
                          capture_output=True, text=True, check=True).stdout


class Env:
    def __init__(self, root: Path, monkeypatch) -> None:
        self.root = root
        self.repo = root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        (self.repo / "README.md").write_text("# Test repository\n", encoding="utf-8")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "init")
        self.task_runtime = root / "task-runtime"
        self.task_runtime.mkdir()
        (root / "worker-state").mkdir()
        self.clock = FakeClock(1_000_000)
        self.controller = Controller(root / "controller-runtime", clock=self.clock)
        self.fleet = Fleet()
        monkeypatch.setenv("HERMES_TASK_ROOTS_IT", str(self.task_runtime))
        monkeypatch.setattr(WorkerDaemon, "_adapter_from_config", staticmethod(self._base_adapter_factory))
        original_execute = BrainstormAdapter.execute

        def recording_execute(adapter_self, task):
            self.fleet.brainstorm_tasks.append(copy.deepcopy(task))
            return original_execute(adapter_self, task)

        monkeypatch.setattr(BrainstormAdapter, "execute", recording_execute)
        self.server = serve(self.controller, port=0, enrollment_tokens={WORKER_ID: WORKER_TOKEN})
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def _base_adapter_factory(self, spec):
        """Replaces only base engine construction; composition stays production code."""
        kinds = {"codex-run": "codex", "claude-agent": "claude"}
        if spec.get("kind") not in kinds:
            raise AssertionError(f"unexpected base adapter kind {spec.get('kind')}")
        return FakeStructuredEngine(kinds[spec["kind"]], self.fleet)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.controller.close()

    def manifest(self, **overrides) -> dict:
        spec = {
            "project_id": "onboarding-docs", "repository": "git@example/onboarding.git", "ref": "main",
            "platform": "linux", "working_directory": str(self.repo), "runtime_directory": str(self.task_runtime),
            "allowed_engines": ["codex", "claude", "hybrid", "brainstorm"], "default_engine": "codex",
            "capabilities": ["git"], "timeout_seconds": 900,
        }
        spec.update(overrides)
        return spec

    def worker_config(self, capabilities=("codex", "claude", "git")) -> dict:
        return {
            "controller_url": self.url, "token": WORKER_TOKEN,
            "state_file": str(self.root / "worker-state" / "state.json"),
            "task_materialization_roots_env": "HERMES_TASK_ROOTS_IT",
            "heartbeat_interval_ms": 10, "retry_backoff_ms": 1, "max_backoff_ms": 5,
            "default_execution_engine": "codex",
            "adapters": {
                "codex": {"kind": "codex-run", "runner_path_env": "UNUSED", "authorized_roots_env": "UNUSED",
                          "execution_profiles": ["hermes", "review", "brainstorm"],
                          "task_types": ["development", "hermes_smoke", "brainstorm"]},
                "claude": {"kind": "claude-agent", "authorized_roots_env": "UNUSED", "cli_path_env": "UNUSED",
                           "execution_profiles": ["claude_smoke", "hermes", "brainstorm"],
                           "task_types": ["claude_smoke", "development", "brainstorm"], "subscription_only": True},
                "hybrid": {"kind": "hybrid", "primary_engine": "claude", "review_engine": "codex", "max_review_rounds": 1},
                "brainstorm": {"kind": "brainstorm", "claude_engine": "claude", "codex_engine": "codex"},
            },
            "worker": {"worker_id": WORKER_ID, "platform": "linux", "environment": "integration test",
                       "capabilities": list(capabilities), "max_concurrent_jobs": 1},
        }

    def worker(self, adapter=None) -> WorkerDaemon:
        daemon = WorkerDaemon(self.worker_config(), adapter)
        daemon.register()
        return daemon

    def enqueue_brainstorm(self, **manifest_overrides) -> tuple[str, dict]:
        self.controller.register_project(self.manifest(**manifest_overrides))
        task = self.controller.build_project_task("onboarding-docs", QUESTION, engine="brainstorm")
        return self.controller.enqueue(task), task

    def task_json(self, job_id: str) -> dict:
        return json.loads(self.controller.db.execute("SELECT task_json FROM jobs WHERE job_id=?", (job_id,)).fetchone()["task_json"])

    def lease(self, job_id: str) -> list[tuple[str, str]]:
        return [(row["lease_id"], row["lease_token"]) for row in
                self.controller.db.execute("SELECT lease_id, lease_token FROM runs WHERE job_id=?", (job_id,))]

    def orchestration(self, job_id: str) -> Path:
        return self.task_runtime / job_id / "orchestration"


@pytest.fixture
def env(tmp_path, monkeypatch):
    environment = Env(tmp_path, monkeypatch)
    yield environment
    environment.close()


def report(env: Env, job_id: str) -> dict:
    return json.loads((env.orchestration(job_id) / "brainstorm-report.json").read_text(encoding="utf-8"))


# --- happy path ------------------------------------------------------------------------------------------------------

def test_happy_path_end_to_end_over_http(env, monkeypatch):
    compressed = []
    real_compress = worker_module.gzip.compress
    monkeypatch.setattr(worker_module.gzip, "compress", lambda data: (compressed.append(len(data)) or real_compress(data)))
    fingerprint_before = repo_fingerprint(env.repo)
    job_id, task = env.enqueue_brainstorm()
    assert (task["execution_engine"], task["execution_profile"], task["task_type"]) == ("brainstorm",) * 3
    assert task["human_gates"] == [] and task["idempotency_policy"] == "safe_retry"
    assert task["brainstorm"]["rubric_id"] == "general-v1" and len(task["brainstorm"]["rubric"]) == 7
    assert "_hermes_runtime" not in task

    daemon = env.worker()
    router = daemon.adapter
    assert isinstance(router, EngineRoutingAdapter) and isinstance(router.adapters["brainstorm"], BrainstormAdapter)
    assert router.adapters["brainstorm"].engines == {"claude": router.adapters["claude"], "codex": router.adapters["codex"]}
    assert isinstance(router.adapters["hybrid"], HybridAdapter)
    assert daemon.once()

    status = env.controller.status(job_id)
    assert status["state"] == "DONE" and status["engine"] == "brainstorm"
    assert status["result"]["status"] == "DONE" and status["result"]["gate"] is None
    assert "RECOMMENDED_FOR_PILOT" in status["result"]["summary"]
    assert status["active_run_id"] is None
    assert [(run["attempt"], run["worker_id"], run["state"]) for run in status["runs"]] == [(1, WORKER_ID, "DONE")]
    assert report(env, job_id)["decision_status"] == "RECOMMENDED_FOR_PILOT"
    assert repo_fingerprint(env.repo) == fingerprint_before
    assert compressed and max(compressed) > 1024   # envelope went through gzip transport

    # the fake engines ran the real six-stage order
    assert [(c["engine"], c["stage"]) for c in env.fleet.calls] == [
        ("codex", "proposals"), ("claude", "proposals"), ("claude", "evaluation"), ("codex", "evaluation"),
        ("codex", "refinement"), ("claude", "validation")]

    # no lease or credential leaks into the public status
    serialized = json.dumps(status)
    for lease_id, lease_token in env.lease(job_id):
        assert lease_id not in serialized and lease_token not in serialized
    assert WORKER_TOKEN not in serialized, "worker token leaked into status"
    assert "lease_id" not in serialized and "lease_token" not in serialized


def test_runtime_context_reaches_brainstorm_but_not_the_controller(env):
    job_id, _task = env.enqueue_brainstorm()
    env.worker().once()
    run = env.controller.status(job_id)["runs"][0]
    assert "_hermes_runtime" not in env.task_json(job_id)
    runtime = env.fleet.brainstorm_tasks[0]["_hermes_runtime"]
    assert runtime == {"job_id": job_id, "run_id": run["run_id"], "attempt": 1}
    adapter_task = json.dumps(env.fleet.brainstorm_tasks[0])
    for lease_id, lease_token in env.lease(job_id):
        assert lease_id not in adapter_task and lease_token not in adapter_task
    assert WORKER_TOKEN not in adapter_task
    assert all("_hermes_runtime" not in call["task"] for call in env.fleet.calls)
    budget = json.loads((env.orchestration(job_id) / "budget.json").read_text())
    assert (budget["run_id"], budget["attempt"]) == (run["run_id"], 1)


def test_artifacts_survive_the_whole_path_to_controller_status(env):
    job_id, _task = env.enqueue_brainstorm()
    env.worker().once()
    status = env.controller.status(job_id)
    artifacts, hashes = status["artifacts"], status["hashes"]
    validate_artifacts(artifacts, hashes)
    assert [item["path"] for item in artifacts] == [f"orchestration/{name}" for name in MINIMUM_ARTIFACTS]
    for item in artifacts:
        data = (env.task_runtime / job_id / item["path"]).read_bytes()
        assert item["bytes"] == len(data) and item["sha256"] == hashlib.sha256(data).hexdigest()
        assert hashes[item["path"]] == item["sha256"]
    inline = [item for item in artifacts if "inline_text" in item]
    assert [item["path"] for item in inline] == ["orchestration/brainstorm-report.md"]
    encoded = inline[0]["inline_text"].encode("utf-8")
    assert len(encoded) == inline[0]["bytes"]
    assert hashlib.sha256(encoded).hexdigest() == inline[0]["sha256"]
    assert encoded == (env.orchestration(job_id) / "brainstorm-report.md").read_bytes()
    assert not any("executions" in path or "stages/" in path for path in hashes)


def test_inconclusive_end_to_end_stays_done(env):
    env.fleet.verdict = "FAIL"
    job_id, _task = env.enqueue_brainstorm()
    env.worker().once()
    status = env.controller.status(job_id)
    assert status["state"] == "DONE" and status["result"]["status"] == "DONE" and status["result"]["gate"] is None
    body = report(env, job_id)
    assert body["decision_status"] == "INCONCLUSIVE"
    assert body["winner"]["title"] == "Idea K0" and body["runner_up"] is not None
    assert "INCONCLUSIVE" in status["result"]["summary"]
    assert [c["stage"] for c in env.fleet.calls].count("refinement") == 1


# --- safe retry through lease expiry --------------------------------------------------------------------------------

def test_expired_lease_retry_reuses_checkpoints_and_resets_budget(env):
    job_id, _task = env.enqueue_brainstorm()
    env.fleet.scripts[("codex", "proposals")] = ["invalid"]          # consumes one retry in attempt 1

    def lose_lease():
        env.clock.advance(LEASE_DURATION_MS + 1_000)
        time.sleep(0.3)                                             # worker heartbeat sees the stale lease (409)
        raise StructuredExecutionError("FAILED", "evaluation", "worker lost its lease mid-run")

    env.fleet.hooks[("claude", "evaluation")] = lose_lease
    daemon = env.worker()
    assert daemon.once()
    attempt_one_budget = json.loads((env.orchestration(job_id) / "budget.json").read_text())
    assert attempt_one_budget["retries_used"] == 1
    assert env.controller.status(job_id)["state"] == "RUNNING"       # nothing was ingested for attempt 1
    assert env.controller.reconcile_expired_leases()
    assert env.controller.status(job_id)["state"] == "QUEUED"

    env.fleet.hooks.clear()
    before = len(env.fleet.calls)
    assert daemon.once()
    status = env.controller.status(job_id)
    assert status["state"] == "DONE" and status["result"]["status"] == "DONE"
    assert [(run["attempt"], run["state"]) for run in status["runs"]] == [(1, "STALE"), (2, "DONE")]
    first, second = status["runs"]
    assert first["run_id"] != second["run_id"]
    runtimes = [task["_hermes_runtime"] for task in env.fleet.brainstorm_tasks]
    assert [(r["job_id"], r["run_id"], r["attempt"]) for r in runtimes] == [
        (job_id, first["run_id"], 1), (job_id, second["run_id"], 2)]
    assert env.fleet.counts(before) == {"claude-evaluation": 1, "codex-evaluation": 1,
                                        "codex-refinement": 1, "claude-validation": 1}
    budget = json.loads((env.orchestration(job_id) / "budget.json").read_text())
    assert (budget["run_id"], budget["attempt"], budget["retries_used"]) == (second["run_id"], 2, 0)
    assert report(env, job_id)["decision_status"] == "RECOMMENDED_FOR_PILOT"


def test_worker_restart_resumes_the_same_claim(env):
    job_id, _task = env.enqueue_brainstorm()
    first = env.worker()
    claim = first.client.claim()
    first._save_state(claim)
    env.fleet.scripts[("codex", "proposals")] = ["invalid"]
    env.fleet.scripts[("claude", "evaluation")] = [StructuredExecutionError("FAILED", "evaluation", "process crash")]
    interrupted = first.adapter.execute(first._prepare_task(claim))   # work done, process dies before ingest
    assert interrupted.status == "FAILED"
    budget_before = json.loads((env.orchestration(job_id) / "budget.json").read_text())

    restarted = env.worker()
    assert restarted.active_claim == claim
    before = len(env.fleet.calls)
    assert restarted.once()
    status = env.controller.status(job_id)
    assert status["state"] == "DONE"
    assert [(run["run_id"], run["attempt"]) for run in status["runs"]] == [(claim["run_id"], claim["attempt"])]
    runtimes = [task["_hermes_runtime"] for task in env.fleet.brainstorm_tasks]
    assert runtimes[0] == runtimes[1] == {"job_id": job_id, "run_id": claim["run_id"], "attempt": claim["attempt"]}
    budget = json.loads((env.orchestration(job_id) / "budget.json").read_text())
    assert budget["started_at"] == budget_before["started_at"]
    assert budget["retries_used"] == budget_before["retries_used"] == 1
    assert "codex-proposals" not in env.fleet.counts(before) and "claude-proposals" not in env.fleet.counts(before)


# --- matching and policy ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("capabilities, claimable", [
    (["codex", "git"], False), (["claude", "git"], False), (["codex", "claude", "git"], True)])
def test_only_workers_with_both_engines_can_claim_brainstorm(env, capabilities, claimable):
    job_id, _task = env.enqueue_brainstorm()
    env.controller.register_worker({"worker_id": "probe", "platform": "linux", "environment": "t",
                                    "capabilities": capabilities, "max_concurrent_jobs": 1})
    claim = env.controller.claim("probe")
    assert (claim is not None) == claimable
    if claim:
        assert claim["job_id"] == job_id


@pytest.mark.parametrize("instruction", [QUESTION, "Brainstorm new ideas for the docs", "Review the architecture",
                                         "Fix the failing test"])
def test_auto_and_default_never_select_brainstorm(env, instruction):
    env.controller.register_project(env.manifest(default_engine="auto"))
    assert env.controller.build_project_task("onboarding-docs", instruction)["execution_engine"] != "brainstorm"
    assert env.controller.build_project_task("onboarding-docs", instruction, engine="auto")["execution_engine"] != "brainstorm"


# --- rejections ---------------------------------------------------------------------------------------------------------

def test_runtime_inside_repo_is_rejected_before_anything_is_written(env, monkeypatch):
    fingerprint_before = repo_fingerprint(env.repo)
    runtime_in_repo = env.repo / "hermes-runtime"
    with pytest.raises(ControllerError, match="runtime_directory"):
        env.controller.register_project(env.manifest(runtime_directory=str(runtime_in_repo)))

    # A task that bypasses the manifest check (older Controller or manual task) is stopped by the worker.
    env.controller.register_project(env.manifest())
    task = env.controller.build_project_task("onboarding-docs", QUESTION, engine="brainstorm")
    task["run_output_dir"] = str(runtime_in_repo / task["job_id"])
    monkeypatch.setenv("HERMES_TASK_ROOTS_IT", f"{env.task_runtime}:{env.repo}")
    job_id = env.controller.enqueue(task)
    env.worker().once()
    status = env.controller.status(job_id)
    assert status["state"] == "BLOCKED" and "working_directory" in status["result"]["summary"]
    assert env.fleet.calls == [] and env.fleet.brainstorm_tasks == []
    assert not runtime_in_repo.exists()
    assert repo_fingerprint(env.repo) == fingerprint_before


class InvalidArtifactsBrainstorm:
    def execute(self, task):
        return AdapterResult(status="DONE", summary="bad artifacts",
                             artifacts=[{"path": "../escape.md", "sha256": "0" * 64, "bytes": 1}],
                             hashes={"../escape.md": "0" * 64})


def test_invalid_artifacts_become_a_terminal_failed_envelope(env):
    job_id, _task = env.enqueue_brainstorm()
    daemon = env.worker(EngineRoutingAdapter({"codex": FakeStructuredEngine("codex", env.fleet),
                                              "brainstorm": InvalidArtifactsBrainstorm()}))
    assert daemon.once()
    status = env.controller.status(job_id)
    assert status["state"] == "FAILED" and status["engine"] == "brainstorm"
    assert status["artifacts"] == [] and status["hashes"] == {}
    assert any("artifact-policy" in item for item in status["result"]["evidence"])
    assert daemon.pending_envelope is None and daemon.active_claim is None


def test_controller_rejects_a_brainstorm_result_with_the_wrong_engine(env):
    job_id, _task = env.enqueue_brainstorm()
    daemon = env.worker()
    claim = daemon.client.claim()
    envelope = {**{key: claim[key] for key in ("job_id", "run_id", "attempt", "lease_id", "lease_token")},
                "worker_id": WORKER_ID, "started_at": 1, "finished_at": 2, "engine": "codex",
                "engine_result": {"status": "DONE", "summary": "x", "gate": None, "completed": [], "remaining": [],
                                  "evidence": []},
                "artifacts": [], "hashes": {}}
    with pytest.raises(TransportError):
        daemon.client.ingest(envelope)
    assert env.controller.status(job_id)["state"] == "RUNNING"
    daemon.client.ingest({**envelope, "engine": "brainstorm"})
    assert env.controller.status(job_id)["state"] == "DONE"


def test_controller_rejects_forged_runtime_in_the_snapshot(env):
    env.controller.register_project(env.manifest())
    task = env.controller.build_project_task("onboarding-docs", QUESTION, engine="brainstorm")
    task["_hermes_runtime"] = {"job_id": "fake", "run_id": "fake", "attempt": 999}
    with pytest.raises(ControllerError, match="reserved"):
        env.controller.enqueue(task)
