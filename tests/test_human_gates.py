import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from hermes_controller.adapters import AdapterResult
from hermes_controller.api import serve
from hermes_controller.clock import FakeClock
from hermes_controller.controller import Controller, ControllerError
from hermes_controller.worker import WorkerDaemon


GATE_A = "INFRASTRUCTURE_APPLY_APPROVAL_REQUIRED"
GATE_B = "LICENSE_REVIEW_REQUIRED"
WORKER = {
    "worker_id": "main-linux",
    "platform": "linux",
    "environment": "test",
    "capabilities": ["codex", "git"],
    "max_concurrent_jobs": 1,
}


def task(*, policy="safe_retry", gates=None, **extra):
    return {
        "brain": {
            "repository": "git@example/brain.git",
            "ref": "main",
            "commit": "a" * 40,
            "task_file": "/tmp/task.md",
        },
        "project": "test",
        "repository": "git@example/project.git",
        "ref": "main",
        "task_type": "development",
        "platform": "linux",
        "required_capabilities": ["codex"],
        "execution_profile": "hermes",
        "human_gates": list(gates or []),
        "idempotency_policy": policy,
        **extra,
    }


def envelope(claim, *, status="WAIT_USER", gate=GATE_A):
    return {
        "job_id": claim["job_id"],
        "run_id": claim["run_id"],
        "attempt": claim["attempt"],
        "worker_id": "main-linux",
        "lease_id": claim["lease_id"],
        "lease_token": claim["lease_token"],
        "started_at": 1,
        "finished_at": 2,
        "engine": "codex",
        "engine_result": {
            "status": status,
            "summary": "gate test",
            "gate": gate,
            "completed": ["preflight"],
            "remaining": ["apply"],
            "evidence": [],
        },
        "artifacts": [],
        "hashes": {},
    }


@pytest.fixture
def core(tmp_path):
    clock = FakeClock(1_000)
    controller = Controller(tmp_path / "controller", clock=clock)
    controller.register_worker(WORKER)
    yield controller, clock
    controller.close()


def wait_for_gate(controller, *, policy="safe_retry", gates=None):
    job = controller.enqueue(task(policy=policy, gates=gates or [GATE_A]))
    claim = controller.claim("main-linux")
    controller.ingest_result(envelope(claim))
    return job, claim


def test_wait_user_exposes_sanitized_gate_summary_and_audit_history(core):
    controller, _ = core
    job, claim = wait_for_gate(controller)

    status = controller.status(job)
    assert status["state"] == "WAIT_USER"
    assert status["gate"] == {"waiting_for": GATE_A, "approved": [], "rejected": []}
    assert controller.gate_history(job) == []
    assert any(
        event["event_type"] == "GATE_WAITING"
        and json.loads(event["payload_json"])["gate"] == GATE_A
        for event in controller.events()
    )
    serialized = json.dumps(status)
    assert claim["lease_token"] not in serialized
    assert "actor" not in serialized and "note" not in serialized


def test_approve_requeues_safe_retry_and_claim_carries_only_approved_gate(core):
    controller, clock = core
    job, first = wait_for_gate(controller)

    resolution = controller.approve_gate(job, GATE_A, actor="deiv", note="approved after review")
    assert resolution["decision"] == "APPROVED"
    assert resolution["disposition"] == "QUEUED"
    assert controller.status(job)["state"] == "QUEUED"
    assert controller.status(job)["gate"] == {"waiting_for": None, "approved": [GATE_A], "rejected": []}

    history = controller.gate_history(job)
    assert history == [{
        "job_id": job,
        "gate": GATE_A,
        "decision": "APPROVED",
        "actor": "deiv",
        "note": "approved after review",
        "decided_at": clock.now(),
        "source_run_id": first["run_id"],
        "disposition": "QUEUED",
    }]

    second = controller.claim("main-linux")
    assert second["attempt"] == 2
    assert second["task"]["_hermes_gate_context"] == {"approved_gates": [GATE_A]}
    serialized_claim = json.dumps(second["task"])
    assert "deiv" not in serialized_claim and "approved after review" not in serialized_claim

    stored = json.loads(controller.db.execute("SELECT task_json FROM jobs WHERE job_id=?", (job,)).fetchone()["task_json"])
    assert not any(key.startswith("_hermes_") for key in stored)


def test_approve_is_idempotent_but_conflicting_resolution_is_rejected(core):
    controller, _ = core
    job, _ = wait_for_gate(controller)
    first = controller.approve_gate(job, GATE_A, actor="deiv", note="ok")
    second = controller.approve_gate(job, GATE_A, actor="deiv", note="ok")
    assert second == first

    with pytest.raises(ControllerError, match="already resolved"):
        controller.reject_gate(job, GATE_A, actor="deiv", note="changed mind")

    approved_events = [e for e in controller.events() if e["event_type"] == "GATE_APPROVED"]
    assert len(approved_events) == 1


def test_reject_cancels_job_and_prevents_future_claim(core):
    controller, _ = core
    job, _ = wait_for_gate(controller)
    result = controller.reject_gate(job, GATE_A, actor="deiv", note="not approved")
    assert result["decision"] == "REJECTED"
    assert result["disposition"] == "CANCELLED"
    status = controller.status(job)
    assert status["state"] == "CANCELLED"
    assert status["gate"] == {"waiting_for": None, "approved": [], "rejected": [GATE_A]}
    assert controller.claim("main-linux") is None


def test_concurrent_conflicting_gate_resolutions_are_atomic(tmp_path):
    root = tmp_path / "controller"
    seed = Controller(root, clock=FakeClock(1_000))
    seed.register_worker(WORKER)
    job, _ = wait_for_gate(seed)
    seed.close()

    barrier = threading.Barrier(2)
    outcomes = []

    def resolve(decision):
        local = Controller(root, clock=FakeClock(2_000))
        try:
            barrier.wait()
            if decision == "APPROVED":
                result = local.approve_gate(job, GATE_A, actor="approver")
            else:
                result = local.reject_gate(job, GATE_A, actor="rejector")
            outcomes.append(("ok", decision, result["disposition"]))
        except ControllerError as exc:
            outcomes.append(("error", decision, str(exc)))
        finally:
            local.close()

    threads = [
        threading.Thread(target=resolve, args=("APPROVED",)),
        threading.Thread(target=resolve, args=("REJECTED",)),
    ]
    [thread.start() for thread in threads]
    [thread.join() for thread in threads]

    winners = [item for item in outcomes if item[0] == "ok"]
    losers = [item for item in outcomes if item[0] == "error"]
    assert len(winners) == 1 and len(losers) == 1
    assert "already resolved" in losers[0][2]

    check = Controller(root, clock=FakeClock(2_000))
    try:
        history = check.gate_history(job)
        assert len(history) == 1
        expected_state = "QUEUED" if history[0]["decision"] == "APPROVED" else "CANCELLED"
        assert check.status(job)["state"] == expected_state
    finally:
        check.close()


def test_manual_reconcile_approval_never_auto_requeues(core):
    controller, _ = core
    job, _ = wait_for_gate(controller, policy="manual_reconcile")
    result = controller.approve_gate(job, GATE_A, actor="deiv")
    assert result["disposition"] == "NEEDS_RECONCILIATION"
    assert controller.status(job)["state"] == "NEEDS_RECONCILIATION"
    assert controller.claim("main-linux") is None


def test_multiple_declared_gates_are_approved_sequentially(core):
    controller, _ = core
    job, _ = wait_for_gate(controller, gates=[GATE_A, GATE_B])
    controller.approve_gate(job, GATE_A, actor="deiv")
    second = controller.claim("main-linux")
    assert second["task"]["_hermes_gate_context"]["approved_gates"] == [GATE_A]

    controller.ingest_result(envelope(second, status="WAIT_USER", gate=GATE_B))
    assert controller.status(job)["gate"]["waiting_for"] == GATE_B
    controller.approve_gate(job, GATE_B, actor="deiv")
    third = controller.claim("main-linux")
    assert third["attempt"] == 3
    assert third["task"]["_hermes_gate_context"]["approved_gates"] == [GATE_A, GATE_B]


def test_resolution_requires_the_current_waiting_gate_and_valid_actor(core):
    controller, _ = core
    job, _ = wait_for_gate(controller, gates=[GATE_A, GATE_B])
    with pytest.raises(ControllerError, match="current waiting gate"):
        controller.approve_gate(job, GATE_B, actor="deiv")
    with pytest.raises(ControllerError, match="actor"):
        controller.approve_gate(job, GATE_A, actor="")


def test_controller_reserves_internal_hermes_namespace(core):
    controller, _ = core
    for key in ("_hermes_runtime", "_hermes_gate_context", "_hermes_anything"):
        with pytest.raises(ControllerError, match="reserved"):
            controller.enqueue(task(**{key: {"fake": True}}))


def test_rerequest_of_already_approved_gate_is_normalized_to_blocked(core):
    controller, _ = core
    job, _ = wait_for_gate(controller)
    controller.approve_gate(job, GATE_A, actor="deiv")
    second = controller.claim("main-linux")
    controller.ingest_result(envelope(second, status="WAIT_USER", gate=GATE_A))
    status = controller.status(job)
    assert status["state"] == "BLOCKED"
    assert status["result"]["status"] == "BLOCKED"
    assert status["result"]["gate"] is None
    assert "already-approved human gate" in status["result"]["summary"]


class GateAwareAdapter:
    def __init__(self):
        self.tasks = []

    def execute(self, task):
        self.tasks.append(task)
        approved = task.get("_hermes_gate_context", {}).get("approved_gates", [])
        text = Path(task["brain"]["task_file"]).read_text(encoding="utf-8")
        if GATE_A in approved:
            assert "Hermes human gate context" in text
            assert GATE_A in text
            return AdapterResult(status="DONE", summary="approved gate resumed", completed=["apply"])
        return AdapterResult(status="WAIT_USER", summary="approval required", gate=GATE_A, completed=["preflight"], remaining=["apply"])


def test_http_worker_e2e_wait_approve_resume_done(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    runtime = tmp_path / "runtime"
    controller = Controller(tmp_path / "controller")
    server = serve(controller, port=0, enrollment_tokens={"main-linux": "token"})
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("HERMES_TEST_ROOTS", str(tmp_path))
    adapter = GateAwareAdapter()
    worker = WorkerDaemon({
        "controller_url": f"http://127.0.0.1:{server.server_port}",
        "token": "token",
        "state_file": str(tmp_path / "worker-state.json"),
        "task_materialization_roots_env": "HERMES_TEST_ROOTS",
        "heartbeat_interval_ms": 10,
        "worker": WORKER,
    }, adapter=adapter)

    instruction = "Perform the harmless test action only after the declared human gate is approved."
    inline = task(
        gates=[GATE_A],
        job_id="gate-e2e",
        brain={
            "repository": "inline://gate-e2e",
            "ref": "gate-e2e",
            "commit": __import__("hashlib").sha256(instruction.encode()).hexdigest(),
        },
        task_text=instruction,
        working_directory=str(repo),
        run_output_dir=str(runtime / "gate-e2e"),
        allowed_paths=[str(repo), str(runtime)],
        execution_profile="hermes",
    )

    try:
        worker.register()
        job = controller.enqueue(inline)
        assert worker.once()
        assert controller.status(job)["state"] == "WAIT_USER"

        controller.approve_gate(job, GATE_A, actor="deiv", note="test approval")
        assert worker.once()
        status = controller.status(job)
        assert status["state"] == "DONE"
        assert [run["attempt"] for run in status["runs"]] == [1, 2]
        assert len(adapter.tasks) == 2
        assert "_hermes_gate_context" not in adapter.tasks[0]
        assert adapter.tasks[1]["_hermes_gate_context"] == {"approved_gates": [GATE_A]}
        gate_task = Path(adapter.tasks[1]["brain"]["task_file"])
        assert gate_task.name == "hermes-task-gates.md"
        assert "test approval" not in gate_task.read_text(encoding="utf-8")
        assert GATE_A in gate_task.read_text(encoding="utf-8")
    finally:
        server.shutdown()
        server.server_close()
        controller.close()


def _operator_request(base_url: str, path: str, token: str, *, body=None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        base_url + path,
        data=data,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="GET" if body is None else "POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status, json.loads(response.read())


def test_http_operator_gate_api_is_separate_from_worker_auth(tmp_path):
    controller = Controller(tmp_path / "controller")
    controller.register_worker(WORKER)
    job, _ = wait_for_gate(controller, gates=[GATE_A, GATE_B])
    server = serve(
        controller, port=0,
        enrollment_tokens={"main-linux": "worker-token"},
        operator_token="operator-token",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        for token in ("", "worker-token"):
            request = urllib.request.Request(
                base + f"/v1/gates/{job}",
                headers={"Authorization": f"Bearer {token}"},
            )
            with pytest.raises(urllib.error.HTTPError) as exc:
                urllib.request.urlopen(request, timeout=5)
            assert exc.value.code == 401

        status_code, shown = _operator_request(base, f"/v1/gates/{job}", "operator-token")
        assert status_code == 200
        assert shown["job_id"] == job
        assert shown["state"] == "WAIT_USER"
        assert shown["gate"]["waiting_for"] == GATE_A
        assert shown["decisions"] == []

        status_code, approved = _operator_request(
            base, "/v1/gates/approve", "operator-token",
            body={"job_id": job, "gate": GATE_A, "actor": "deiv", "note": "http approval"},
        )
        assert status_code == 200
        assert approved["decision"] == "APPROVED"
        assert approved["disposition"] == "QUEUED"
        assert controller.status(job)["state"] == "QUEUED"

        second = controller.claim("main-linux")
        controller.ingest_result(envelope(second, status="WAIT_USER", gate=GATE_B))
        status_code, rejected = _operator_request(
            base, "/v1/gates/reject", "operator-token",
            body={"job_id": job, "gate": GATE_B, "actor": "deiv", "note": "http rejection"},
        )
        assert status_code == 200
        assert rejected["decision"] == "REJECTED"
        assert rejected["disposition"] == "CANCELLED"
        assert controller.status(job)["state"] == "CANCELLED"

        db_dump = "\n".join(
            str(tuple(row))
            for row in controller.db.execute(
                "SELECT job_id, gate, decision, actor, note, source_run_id, disposition FROM gate_decisions"
            ).fetchall()
        )
        assert "operator-token" not in db_dump
        assert "worker-token" not in json.dumps(controller.status(job))
    finally:
        server.shutdown()
        server.server_close()
        controller.close()


def test_http_gate_api_is_disabled_without_operator_token(tmp_path):
    controller = Controller(tmp_path / "controller")
    controller.register_worker(WORKER)
    job, _ = wait_for_gate(controller)
    server = serve(controller, port=0, enrollment_tokens={"main-linux": "worker-token"})
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        request = urllib.request.Request(
            base + f"/v1/gates/{job}",
            headers={"Authorization": "Bearer worker-token"},
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(request, timeout=5)
        assert exc.value.code == 401
    finally:
        server.shutdown()
        server.server_close()
        controller.close()


def test_cli_gate_approve_and_status(tmp_path):
    root = tmp_path / "controller"
    controller = Controller(root)
    controller.register_worker(WORKER)
    job, _ = wait_for_gate(controller)
    controller.close()

    base = [sys.executable, "-m", "hermes_controller", "--runtime-root", str(root), "gate"]
    approved = subprocess.run(
        [*base, "approve", job, GATE_A, "--actor", "deiv", "--note", "cli approval"],
        text=True, capture_output=True, check=True,
    )
    payload = json.loads(approved.stdout)
    assert payload["decision"] == "APPROVED"
    assert payload["disposition"] == "QUEUED"

    shown = subprocess.run([*base, "status", job], text=True, capture_output=True, check=True)
    gate_status = json.loads(shown.stdout)
    assert gate_status["job_id"] == job
    assert gate_status["decisions"][0]["actor"] == "deiv"
    assert gate_status["decisions"][0]["note"] == "cli approval"
