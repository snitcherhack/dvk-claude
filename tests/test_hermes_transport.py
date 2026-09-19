from __future__ import annotations

import json
import threading
import time

import pytest

import hermes_controller.controller as controller_module
from hermes_controller.adapters import ClaudeAgentAdapter, EngineRoutingAdapter, HybridAdapter, MockAdapter
from hermes_controller.api import serve
from hermes_controller.clock import FakeClock
from hermes_controller.controller import Controller, StaleResultError
from hermes_controller.worker import HTTPControllerClient, TransportError, WorkerDaemon

MAIN = {"worker_id": "main-linux", "platform": "linux", "environment": "WSL/Kali", "capabilities": ["codex", "git", "python", "tests", "image_qa", "network"], "max_concurrent_jobs": 1}
WINDOWS = {"worker_id": "windows-render", "platform": "windows", "environment": "Windows", "capabilities": ["ffmpeg", "final_render", "mp4_qa", "artifact_hash"], "max_concurrent_jobs": 1}

def task(*, platform="linux", capabilities=None, policy="safe_retry", gates=None):
    return {"brain": {"repository": "git@example/brain.git", "ref": "main", "commit": "a" * 40, "task_file": "task.md"}, "project": "test", "repository": "git@example/project.git", "ref": "main", "task_type": "development" if platform == "linux" else "windows_render_qa", "platform": platform, "required_capabilities": capabilities or ["codex"], "execution_profile": "hermes-workspace-write", "human_gates": gates or [], "idempotency_policy": policy}

def envelope(claim):
    return {"job_id": claim["job_id"], "run_id": claim["run_id"], "attempt": claim["attempt"], "worker_id": "main-linux", "lease_id": claim["lease_id"], "lease_token": claim["lease_token"], "started_at": 1, "finished_at": 2, "codex_result": {"status": "DONE", "summary": "test", "gate": None, "completed": [], "remaining": [], "evidence": []}, "artifacts": [], "hashes": {}}


@pytest.fixture
def api(tmp_path):
    controller = Controller(tmp_path)
    server = serve(controller, port=0, enrollment_tokens={"main-linux": "test-main", "windows-render": "test-windows"})
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    yield controller, url, tmp_path
    server.shutdown(); server.server_close(); controller.close()


def daemon(url, root, worker=MAIN, token="test-main", **extra):
    return WorkerDaemon({"controller_url": url, "token": token, "state_file": str(root / f"{worker['worker_id']}.json"), "heartbeat_interval_ms": 10, "retry_backoff_ms": 1, "max_backoff_ms": 5, "worker": worker, **extra})


def test_a_register_api_online_and_g_bad_token(api):
    controller, url, root = api
    d = daemon(url, root); d.register()
    assert controller.worker_status("main-linux")["state"] == "ONLINE"
    with pytest.raises(TransportError): HTTPControllerClient(url, "main-linux", "wrong").heartbeat_worker()


def test_b_d_done_e_wait_user_and_f_incompatible(api):
    controller, url, root = api
    linux, windows = daemon(url, root), daemon(url, root, WINDOWS, "test-windows")
    linux.register(); windows.register()
    done = controller.enqueue(task()); wait = controller.enqueue({**task(gates=["WINDOWS_FINAL_RENDER_REQUIRED"]), "mock": {"status": "WAIT_USER", "gate": "WINDOWS_FINAL_RENDER_REQUIRED"}})
    assert linux.once() and controller.status(done)["state"] == "DONE"
    assert linux.once() and controller.status(wait)["state"] == "WAIT_USER"
    other = controller.enqueue(task(platform="windows", capabilities=["final_render"], policy="manual_reconcile"))
    assert linux.client.claim() is None
    assert windows.client.claim()["job_id"] == other


def test_c_long_mock_renews_worker_and_lease(api, monkeypatch):
    controller, url, root = api
    monkeypatch.setattr(controller_module, "LEASE_DURATION_MS", 1_000)
    d = daemon(url, root); d.register()
    controller.enqueue({**task(), "mock": {"delay_ms": 60}})
    d.once()
    kinds = [event["event_type"] for event in controller.events()]
    assert "LEASE_RENEWED" in kinds and "WORKER_HEARTBEAT" in kinds


def test_h_loss_preserves_local_run_state(api):
    controller, _url, root = api
    controller.register_worker(MAIN)
    claim = controller.claim("main-linux") if controller.enqueue(task()) else None
    d = daemon("http://127.0.0.1:1", root)
    d.active_claim = claim; d._save_state(claim)
    with pytest.raises(TransportError): d.once()
    assert d.state_path.exists() and d._load_state()["claim"]["run_id"] == claim["run_id"]


def test_i_restart_reconciles_persisted_epoch_lease(tmp_path):
    clock = FakeClock(1_000); first = Controller(tmp_path, clock=clock)
    first.register_worker(MAIN); job = first.enqueue(task()); first.claim("main-linux"); first.close()
    clock.advance(180_001); second = Controller(tmp_path, clock=clock)
    assert second.reconcile_expired_leases() and second.status(job)["state"] == "QUEUED"
    second.close()


def test_j_duplicate_result_and_k_old_lease_rejected(api):
    controller, url, root = api
    d = daemon(url, root); d.register(); controller.enqueue(task())
    claim = d.client.claim(); item = envelope(claim)
    d.client.ingest(item); d.client.ingest(item)
    assert controller.status(claim["job_id"])["state"] == "DONE"
    with pytest.raises(TransportError): d.client.ingest({**item, "lease_id": "old"})


def test_worker_emits_engine_neutral_result_envelope(api):
    controller, url, root = api
    d = daemon(url, root)
    d.register()
    job = controller.enqueue(task())
    assert d.once() and controller.status(job)["state"] == "DONE"
    row = controller.db.execute("SELECT result_json FROM runs WHERE job_id=?", (job,)).fetchone()
    payload = json.loads(row["result_json"])
    assert payload["engine"] == "codex"
    assert "engine_result" in payload and "codex_result" not in payload


def test_native_worker_emits_native_engine(api):
    controller, url, root = api
    d = daemon(url, root, WINDOWS, "test-windows")
    d.register()
    job = controller.enqueue(task(platform="windows", capabilities=["final_render"], policy="manual_reconcile"))
    assert d.once() and controller.status(job)["state"] == "DONE"
    row = controller.db.execute("SELECT result_json FROM runs WHERE job_id=?", (job,)).fetchone()
    payload = json.loads(row["result_json"])
    assert payload["engine"] == "native"


def test_worker_config_supports_multiple_execution_engines(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_TEST_ROOTS", str(tmp_path))
    cli = tmp_path / "claude"
    cli.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_TEST_CLAUDE_CLI", str(cli))
    config = {
        "controller_url": "http://127.0.0.1:1", "token": "test-main",
        "state_file": str(tmp_path / "worker.json"), "worker": MAIN,
        "default_execution_engine": "codex",
        "adapters": {
            "codex": {"kind": "mock"},
            "claude": {"kind": "claude-agent", "authorized_roots_env": "HERMES_TEST_ROOTS", "cli_path_env": "HERMES_TEST_CLAUDE_CLI"},
            "hybrid": {"kind": "hybrid", "primary_engine": "claude", "review_engine": "codex", "max_review_rounds": 1},
        },
    }
    worker = WorkerDaemon(config)
    assert isinstance(worker.adapter, EngineRoutingAdapter)
    assert worker.adapter.default_engine == "codex"
    assert set(worker.adapter.adapters) == {"codex", "claude", "hybrid"}
    assert isinstance(worker.adapter.adapters["claude"], ClaudeAgentAdapter)
    assert isinstance(worker.adapter.adapters["hybrid"], HybridAdapter)
    assert worker.adapter.adapters["claude"].cli_path == cli.resolve()
    claude_default = WorkerDaemon({**config, "default_execution_engine": "claude"})
    assert claude_default.adapter.default_engine == "claude"


def test_l_two_daemons_one_claim_and_m_non_idempotent_not_redistributed(api, monkeypatch):
    controller, url, root = api
    a, b = daemon(url, root), daemon(url, root / "b")
    a.register(); controller.enqueue(task())
    outcomes = []
    threads = [threading.Thread(target=lambda: outcomes.append(a.client.claim())), threading.Thread(target=lambda: outcomes.append(b.client.claim()))]
    [thread.start() for thread in threads]; [thread.join() for thread in threads]
    assert len([x for x in outcomes if x]) == 1
    controller.register_worker(WINDOWS); job = controller.enqueue(task(platform="windows", capabilities=["final_render"], policy="manual_reconcile")); claim = controller.claim("windows-render")
    controller.db.execute("UPDATE runs SET lease_expires_at=0 WHERE run_id=?", (claim["run_id"],))
    controller.reconcile_expired_leases()
    assert controller.status(job)["state"] == "NEEDS_RECONCILIATION"


def test_worker_materializes_inline_task_inside_configured_root(api, monkeypatch):
    controller, url, root = api
    material_root = root / "materialized"
    monkeypatch.setenv("HERMES_TASK_ROOTS", str(material_root))
    d = daemon(
        url,
        root,
        task_materialization_roots_env="HERMES_TASK_ROOTS",
    )
    d.register()
    text = "Read-only inline task."
    import hashlib
    inline = {
        **task(),
        "job_id": "inline-job",
        "brain": {
            "repository": "inline://hermes-projects/test",
            "ref": "test",
            "commit": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        },
        "task_text": text,
        "run_output_dir": str(material_root / "inline-job"),
        "allowed_paths": [str(material_root)],
        "execution_engine": "codex",
    }
    job = controller.enqueue(inline)
    assert d.once()
    assert controller.status(job)["state"] == "DONE"
    task_file = material_root / "inline-job" / "hermes-task.md"
    assert task_file.read_text(encoding="utf-8") == text
    assert task_file.stat().st_mode & 0o777 == 0o600


def test_worker_blocks_inline_task_outside_materialization_root(api, monkeypatch):
    controller, url, root = api
    material_root = root / "materialized"
    monkeypatch.setenv("HERMES_TASK_ROOTS", str(material_root))
    d = daemon(
        url,
        root,
        task_materialization_roots_env="HERMES_TASK_ROOTS",
    )
    d.register()
    text = "Do not execute."
    import hashlib
    inline = {
        **task(),
        "job_id": "outside-inline-job",
        "brain": {
            "repository": "inline://hermes-projects/test",
            "ref": "test",
            "commit": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        },
        "task_text": text,
        "run_output_dir": str(root / "outside" / "run"),
        "allowed_paths": [str(root / "outside")],
        "execution_engine": "codex",
    }
    job = controller.enqueue(inline)
    assert d.once()
    assert controller.status(job)["state"] == "BLOCKED"
    assert not (root / "outside" / "run" / "hermes-task.md").exists()
