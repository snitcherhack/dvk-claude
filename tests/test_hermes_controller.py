from __future__ import annotations

import threading

import pytest

from hermes_controller.clock import FakeClock
from hermes_controller.controller import Controller, ControllerError, StaleResultError


MAIN = {"worker_id": "main-linux", "platform": "linux", "environment": "WSL/Kali", "capabilities": ["codex", "git", "python", "tests", "image_qa", "network"], "max_concurrent_jobs": 1}
WINDOWS = {"worker_id": "windows-render", "platform": "windows", "environment": "Windows", "capabilities": ["ffmpeg", "final_render", "mp4_qa", "artifact_hash"], "max_concurrent_jobs": 1}


def task(*, platform="linux", capabilities=None, policy="safe_retry", depends_on=None, gates=None):
    return {
        "brain": {"repository": "git@example/brain.git", "ref": "main", "commit": "a" * 40, "task_file": "proyectos/youtube/TAREA_ACTIVA.md"},
        "project": "youtube", "repository": "git@example/video.git", "ref": "feat/example",
        "task_type": "development" if platform == "linux" else "windows_render_qa", "platform": platform,
        "required_capabilities": capabilities or ["codex"], "execution_profile": "hermes-workspace-write",
        "human_gates": gates or [], "idempotency_policy": policy, **({"depends_on": depends_on} if depends_on else {}),
    }


@pytest.fixture
def core(tmp_path):
    clock = FakeClock(1_000)
    controller = Controller(tmp_path, clock=clock)
    controller.register_worker(MAIN)
    controller.register_worker(WINDOWS)
    yield controller, clock, tmp_path
    controller.close()


def envelope(claim, status="DONE", gate=None, worker_id="main-linux"):
    return {
        "job_id": claim["job_id"], "run_id": claim["run_id"], "attempt": claim["attempt"],
        "worker_id": worker_id, "lease_id": claim["lease_id"], "lease_token": claim["lease_token"],
        "started_at": 1_000, "finished_at": 1_001,
        "codex_result": {"status": status, "summary": "deterministic test", "gate": gate, "completed": [], "remaining": [], "evidence": []},
        "artifacts": [], "hashes": {},
    }


def test_a_enqueue_claim_heartbeat_done(core):
    controller, _, _ = core
    job = controller.enqueue(task())
    claim = controller.claim("main-linux")
    controller.heartbeat_worker("main-linux")
    controller.heartbeat_run(claim["run_id"], claim["lease_id"], claim["lease_token"])
    controller.ingest_result(envelope(claim))
    assert controller.status(job)["state"] == "DONE"


def test_engine_result_envelope_and_legacy_codex_result_are_both_supported(core):
    controller, _, _ = core
    modern_job = controller.enqueue(task())
    modern_claim = controller.claim("main-linux")
    modern = envelope(modern_claim)
    modern["engine"] = "codex"
    modern["engine_result"] = modern.pop("codex_result")
    controller.ingest_result(modern)
    assert controller.status(modern_job)["state"] == "DONE"

    legacy_job = controller.enqueue({**task(), "idempotency_key": "legacy-result-test"})
    legacy_claim = controller.claim("main-linux")
    controller.ingest_result(envelope(legacy_claim))
    assert controller.status(legacy_job)["state"] == "DONE"


def test_legacy_result_is_supported_for_native_worker(core):
    controller, _, _ = core
    job = controller.enqueue(task(platform="windows", capabilities=["final_render"], policy="manual_reconcile"))
    claim = controller.claim("windows-render")
    controller.ingest_result(envelope(claim, worker_id="windows-render"))
    assert controller.status(job)["state"] == "DONE"


def test_result_envelope_rejects_ambiguous_payload(core):
    controller, _, _ = core
    controller.enqueue(task())
    claim = controller.claim("main-linux")
    ambiguous = envelope(claim)
    ambiguous["engine"] = "codex"
    ambiguous["engine_result"] = dict(ambiguous["codex_result"])
    with pytest.raises(ControllerError):
        controller.ingest_result(ambiguous)


def test_result_engine_must_match_task(core):
    controller, _, _ = core
    controller.enqueue(task())
    claim = controller.claim("main-linux")
    wrong_engine = envelope(claim)
    wrong_engine["engine"] = "claude"
    wrong_engine["engine_result"] = wrong_engine.pop("codex_result")
    with pytest.raises(ControllerError, match="does not match task"):
        controller.ingest_result(wrong_engine)


def test_worker_is_offline_after_configured_interval(core):
    controller, clock, _ = core
    clock.advance(90_001)
    assert controller.worker_status("main-linux")["state"] == "OFFLINE"


def test_b_wait_user_gate_is_preserved(core):
    controller, _, _ = core
    job = controller.enqueue(task(gates=["WINDOWS_FINAL_RENDER_REQUIRED"]))
    claim = controller.claim("main-linux")
    controller.ingest_result(envelope(claim, "WAIT_USER", "WINDOWS_FINAL_RENDER_REQUIRED"))
    assert controller.status(job)["state"] == "WAIT_USER"


def test_c_dependent_job_becomes_eligible_for_windows(core):
    controller, _, _ = core
    parent = controller.enqueue(task())
    child = controller.enqueue(task(platform="windows", capabilities=["ffmpeg", "final_render", "mp4_qa", "artifact_hash"], policy="manual_reconcile", depends_on=parent))
    assert controller.claim("windows-render") is None
    claimed_parent = controller.claim("main-linux")
    controller.ingest_result(envelope(claimed_parent))
    claim = controller.claim("windows-render")
    assert claim and claim["job_id"] == child


def test_d_expired_idempotent_job_creates_new_attempt(core):
    controller, clock, _ = core
    job = controller.enqueue(task(policy="safe_retry"))
    first = controller.claim("main-linux")
    clock.advance(180_001)
    controller.reconcile_expired_leases()
    second = controller.claim("main-linux")
    assert controller.status(job)["state"] == "RUNNING"
    assert second["attempt"] == first["attempt"] + 1


def test_e_expired_non_idempotent_job_is_not_redistributed(core):
    controller, clock, _ = core
    job = controller.enqueue(task(policy="manual_reconcile"))
    controller.claim("main-linux")
    clock.advance(180_001)
    controller.reconcile_expired_leases()
    assert controller.status(job)["state"] == "NEEDS_RECONCILIATION"
    assert controller.claim("main-linux") is None


def test_f_late_old_attempt_cannot_change_current_attempt(core):
    controller, clock, _ = core
    job = controller.enqueue(task())
    old = controller.claim("main-linux")
    clock.advance(180_001)
    controller.reconcile_expired_leases()
    current = controller.claim("main-linux")
    with pytest.raises(StaleResultError):
        controller.ingest_result(envelope(old, "FAILED"))
    assert controller.status(job)["active_run_id"] == current["run_id"]


def test_g_simultaneous_claims_are_atomic(core):
    _, _, root = core
    seeded = Controller(root, clock=FakeClock(1_000))
    job = seeded.enqueue(task())
    seeded.close()
    barrier, outcomes = threading.Barrier(2), []

    def claim(worker_id):
        local = Controller(root, clock=FakeClock(1_000))
        barrier.wait()
        outcomes.append(local.claim(worker_id))
        local.close()

    threads = [threading.Thread(target=claim, args=("main-linux",)), threading.Thread(target=claim, args=("main-linux",))]
    [thread.start() for thread in threads]
    [thread.join() for thread in threads]
    claims = [outcome for outcome in outcomes if outcome]
    assert len(claims) == 1 and claims[0]["job_id"] == job


def test_execution_engine_requires_matching_capabilities(core):
    controller, _, _ = core
    with pytest.raises(ControllerError):
        controller.enqueue({**task(capabilities=["codex"]), "execution_engine": "claude"})
    with pytest.raises(ControllerError):
        controller.enqueue({**task(capabilities=["claude"]), "execution_engine": "hybrid"})
    claude_job = controller.enqueue({**task(capabilities=["claude"]), "execution_engine": "claude"})
    hybrid_job = controller.enqueue({**task(capabilities=["codex", "claude"]), "execution_engine": "hybrid"})
    assert controller.status(claude_job)["state"] == "QUEUED"
    assert controller.status(hybrid_job)["state"] == "QUEUED"


def test_h_youtube_gate_never_causes_publication(core):
    controller, _, _ = core
    job = controller.enqueue(task(gates=["YOUTUBE_PUBLICATION_APPROVAL"]))
    claim = controller.claim("main-linux")
    controller.ingest_result(envelope(claim, "WAIT_USER", "YOUTUBE_PUBLICATION_APPROVAL"))
    assert controller.status(job)["state"] == "WAIT_USER"
    assert all(event["event_type"] != "PUBLICATION_TRIGGERED" for event in controller.events())

def test_controller_rejects_gate_on_non_wait_user_result(core):
    controller, _, _ = core
    controller.enqueue(task())
    claim = controller.claim("main-linux")
    item = envelope(claim)
    item["codex_result"]["gate"] = "invented-gate"
    with pytest.raises(ControllerError, match="non-WAIT_USER"):
        controller.ingest_result(item)


def test_controller_rejects_undeclared_wait_user_gate(core):
    controller, _, _ = core
    controller.enqueue(task())
    claim = controller.claim("main-linux")
    item = envelope(claim, status="WAIT_USER", gate="invented-gate")
    with pytest.raises(ControllerError, match="declared human gate"):
        controller.ingest_result(item)
