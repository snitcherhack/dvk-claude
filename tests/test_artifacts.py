from __future__ import annotations

import hashlib
import json
import threading

import pytest

from hermes_controller.adapters import AdapterResult, HybridAdapter, MockAdapter
from hermes_controller.api import serve
from hermes_controller.artifacts import MAX_INLINE_TEXT_BYTES, text_artifact, validate_artifacts
from hermes_controller.clock import FakeClock
from hermes_controller.controller import Controller, ControllerError
from hermes_controller.worker import WorkerDaemon


MAIN = {"worker_id": "main-linux", "platform": "linux", "environment": "WSL", "capabilities": ["codex", "claude", "git"], "max_concurrent_jobs": 1}
REPORT_PATH = "orchestration/brainstorm-report.md"


def task(**extra):
    return {
        "brain": {"repository": "git@example/brain.git", "ref": "main", "commit": "a" * 40, "task_file": "task.md"},
        "project": "test", "repository": "git@example/project.git", "ref": "main", "task_type": "development",
        "platform": "linux", "required_capabilities": ["codex"], "execution_profile": "hermes",
        "human_gates": [], "idempotency_policy": "safe_retry", **extra,
    }


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def enriched(text="# Report\n\nÑandú ✓\n", path=REPORT_PATH, **overrides):
    artifact = {"path": path, "sha256": sha(text), "bytes": len(text.encode("utf-8")),
                "media_type": "text/markdown", "inline_text": text}
    artifact.update(overrides)
    return artifact


@pytest.fixture
def core(tmp_path):
    controller = Controller(tmp_path, clock=FakeClock(1_000))
    controller.register_worker(MAIN)
    yield controller
    controller.close()


def claimed_envelope(controller, *, artifacts, hashes, engine="codex"):
    controller.enqueue(task())
    claim = controller.claim("main-linux")
    return claim["job_id"], {
        "job_id": claim["job_id"], "run_id": claim["run_id"], "attempt": claim["attempt"],
        "worker_id": "main-linux", "lease_id": claim["lease_id"], "lease_token": claim["lease_token"],
        "started_at": 1, "finished_at": 2, "engine": engine,
        "engine_result": {"status": "DONE", "summary": "ok", "gate": None, "completed": [], "remaining": [], "evidence": []},
        "artifacts": artifacts, "hashes": hashes,
    }


# --- AdapterResult -----------------------------------------------------------------

def test_adapter_result_without_artifacts_keeps_legacy_surface():
    result = AdapterResult(status="DONE", summary="s")
    assert result.artifacts is None and result.hashes is None
    assert result.result() == {"status": "DONE", "summary": "s", "gate": None,
                               "completed": [], "remaining": [], "evidence": []}


def test_adapter_result_with_artifacts_keeps_six_public_fields():
    artifact = enriched()
    result = AdapterResult(status="DONE", summary="s", artifacts=[artifact], hashes={REPORT_PATH: artifact["sha256"]})
    assert set(result.result()) == {"status", "summary", "gate", "completed", "remaining", "evidence"}
    assert result.artifacts == [artifact]
    assert result.hashes == {REPORT_PATH: artifact["sha256"]}


def test_existing_adapters_return_no_artifacts(tmp_path):
    assert MockAdapter().execute(task()).artifacts is None

    class Stage:
        def execute(self, _task):
            return AdapterResult(status="DONE", summary="stage")

    task_file = tmp_path / "task.md"
    task_file.write_text("x", encoding="utf-8")
    hybrid = HybridAdapter(Stage(), Stage()).execute(task(
        brain={"repository": "r", "ref": "m", "commit": "c", "task_file": str(task_file)},
        run_output_dir=str(tmp_path / "out"), allowed_paths=[str(tmp_path)],
    ))
    assert hybrid.status == "DONE"
    assert hybrid.artifacts is None and hybrid.hashes is None


# --- text_artifact helper ------------------------------------------------------------

def test_text_artifact_builds_consistent_enriched_artifact():
    text = "línea ✓\n"
    artifact = text_artifact(REPORT_PATH, text, media_type="text/markdown", inline=True)
    assert artifact == enriched(text)
    validate_artifacts([artifact], {REPORT_PATH: artifact["sha256"]})
    without_inline = text_artifact(REPORT_PATH, text, inline=False)
    assert "inline_text" not in without_inline and "media_type" not in without_inline


def test_text_artifact_refuses_inline_text_over_limit():
    with pytest.raises(ValueError, match="32768"):
        text_artifact(REPORT_PATH, "x" * (MAX_INLINE_TEXT_BYTES + 1), inline=True)


# --- validation: legacy compatibility ---------------------------------------------------

@pytest.mark.parametrize("artifacts, hashes", [
    ([], {}),
    ([{"path": "report.json"}], {"report.json": "sha256:abc"}),
    ([{"path": "/abs/legacy.log"}, "free-form", 3], {}),
    ([{"path": "report.json"}], {}),
    ([{"path": "report.json", "kind": "log"}], {"other": "anything"}),
])
def test_legacy_artifacts_are_not_validated_retroactively(artifacts, hashes):
    validate_artifacts(artifacts, hashes)


# --- validation: enriched artifacts ---------------------------------------------------------

def test_enriched_artifact_without_inline_text_is_valid():
    artifact = {"path": "orchestration/ranking.json", "sha256": "b" * 64, "bytes": 10, "media_type": "application/json"}
    validate_artifacts([artifact], {"orchestration/ranking.json": "b" * 64})


def test_inline_text_at_exact_limit_is_valid():
    text = "a" * MAX_INLINE_TEXT_BYTES
    artifact = enriched(text)
    validate_artifacts([artifact], {REPORT_PATH: artifact["sha256"]})


@pytest.mark.parametrize("artifact, message", [
    (enriched(path="/etc/passwd"), "relative"),
    (enriched(path="C:/Windows/x.md"), "relative"),
    (enriched(path="\\\\server\\share\\x.md"), "relative"),
    (enriched(path="../escape.md"), r"\.\."),
    (enriched(path="orchestration/../../escape.md"), r"\.\."),
    (enriched(path="orchestration\\..\\escape.md"), "backslash"),
    (enriched(path=""), "path"),
    (enriched(path="a/\x00.md"), "path"),
    (enriched(bytes=-1), "bytes"),
    (enriched(bytes=True), "bytes"),
    (enriched(bytes="12"), "bytes"),
    (enriched(sha256="B" * 64), "sha256"),
    (enriched(sha256="sha256:" + "b" * 64), "sha256"),
    (enriched(sha256="b" * 63), "sha256"),
    (enriched(media_type=""), "media_type"),
    (enriched(media_type=7), "media_type"),
    (enriched(inline_text=5), "inline_text"),
    (enriched(path="orchestration/./report.md"), "segments"),
    (enriched(path="orchestration//report.md"), "segments"),
    (enriched(extra="field"), "fields"),
])
def test_enriched_artifact_field_validation(artifact, message):
    with pytest.raises(ValueError, match=message):
        validate_artifacts([artifact], {artifact.get("path") or "x": artifact.get("sha256")})


def test_enriched_artifact_requires_sha256_and_bytes():
    for missing in ("sha256", "bytes"):
        artifact = enriched()
        artifact.pop(missing)
        with pytest.raises(ValueError, match=missing):
            validate_artifacts([artifact], {REPORT_PATH: sha("# Report\n\nÑandú ✓\n")})


def test_inline_text_over_limit_is_rejected():
    artifact = enriched("é" * (MAX_INLINE_TEXT_BYTES // 2 + 1))
    with pytest.raises(ValueError, match="32768"):
        validate_artifacts([artifact], {REPORT_PATH: artifact["sha256"]})


def test_inline_text_must_be_encodable_utf8():
    artifact = enriched(inline_text="\ud800", sha256="c" * 64, bytes=3)
    with pytest.raises(ValueError, match="UTF-8"):
        validate_artifacts([artifact], {REPORT_PATH: "c" * 64})


def test_inline_text_bytes_mismatch_is_rejected():
    artifact = enriched(bytes=1)
    with pytest.raises(ValueError, match="bytes"):
        validate_artifacts([artifact], {REPORT_PATH: artifact["sha256"]})


def test_inline_text_sha256_mismatch_is_rejected():
    artifact = enriched(sha256=sha("different text"))
    with pytest.raises(ValueError, match="sha256"):
        validate_artifacts([artifact], {REPORT_PATH: artifact["sha256"]})


def test_enriched_artifact_hash_map_must_match():
    artifact = enriched()
    with pytest.raises(ValueError, match="hashes"):
        validate_artifacts([artifact], {REPORT_PATH: "d" * 64})
    with pytest.raises(ValueError, match="hashes"):
        validate_artifacts([artifact], {})


def test_enriched_artifact_paths_must_be_unique():
    artifact = enriched()
    with pytest.raises(ValueError, match="duplicate"):
        validate_artifacts([artifact, dict(artifact)], {REPORT_PATH: artifact["sha256"]})


# --- Controller ingest and status --------------------------------------------------------------

def test_controller_accepts_legacy_artifact_and_exposes_it_unchanged(core):
    job_id, item = claimed_envelope(core, artifacts=[{"path": "report.json"}], hashes={"report.json": "sha256:abc"})
    core.ingest_result(item)
    status = core.status(job_id)
    assert status["artifacts"] == [{"path": "report.json"}]
    assert status["hashes"] == {"report.json": "sha256:abc"}


def test_controller_status_returns_inline_text(core):
    artifact = enriched()
    job_id, item = claimed_envelope(core, artifacts=[artifact], hashes={REPORT_PATH: artifact["sha256"]})
    core.ingest_result(item)
    status = core.status(job_id)
    assert status["artifacts"] == [artifact]
    assert status["artifacts"][0]["inline_text"] == "# Report\n\nÑandú ✓\n"
    assert status["hashes"] == {REPORT_PATH: artifact["sha256"]}


@pytest.mark.parametrize("artifact, hashes", [
    (enriched(path="/abs/report.md"), None),
    (enriched(path="../report.md"), None),
    (enriched("x" * (MAX_INLINE_TEXT_BYTES + 1)), None),
    (enriched(bytes=3), None),
    (enriched(sha256=sha("tampered")), None),
    (enriched(), {REPORT_PATH: "e" * 64}),
])
def test_controller_rejects_invalid_enriched_artifacts(core, artifact, hashes):
    job_id, item = claimed_envelope(core, artifacts=[artifact], hashes=hashes or {artifact["path"]: artifact["sha256"]})
    with pytest.raises(ControllerError, match="artifact"):
        core.ingest_result(item)
    assert core.status(job_id)["state"] == "RUNNING"


# --- worker envelope ------------------------------------------------------------------------------

class FixedAdapter:
    def __init__(self, result: AdapterResult) -> None:
        self.result = result

    def execute(self, _task):
        return self.result


@pytest.fixture
def api(tmp_path):
    controller = Controller(tmp_path)
    server = serve(controller, port=0, enrollment_tokens={"main-linux": "test-main"})
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield controller, f"http://127.0.0.1:{server.server_port}", tmp_path
    server.shutdown()
    server.server_close()
    controller.close()


def worker(url, root, adapter):
    return WorkerDaemon({"controller_url": url, "token": "test-main", "state_file": str(root / "state.json"),
                         "heartbeat_interval_ms": 10, "retry_backoff_ms": 1, "max_backoff_ms": 5, "worker": MAIN}, adapter)


def stored_envelope(controller, job_id):
    row = controller.db.execute("SELECT result_json FROM runs WHERE job_id=?", (job_id,)).fetchone()
    return json.loads(row["result_json"])


def test_worker_defaults_to_empty_artifacts_and_hashes(api):
    controller, url, root = api
    d = worker(url, root, FixedAdapter(AdapterResult(status="DONE", summary="plain")))
    d.register()
    job = controller.enqueue(task())
    assert d.once()
    payload = stored_envelope(controller, job)
    assert payload["artifacts"] == [] and payload["hashes"] == {}


def test_worker_copies_artifacts_and_hashes_into_envelope(api):
    controller, url, root = api
    artifact = enriched()
    legacy = {"path": "legacy.log"}
    result = AdapterResult(status="DONE", summary="report", artifacts=[artifact, legacy],
                           hashes={REPORT_PATH: artifact["sha256"], "legacy.log": "sha256:abc"})
    d = worker(url, root, FixedAdapter(result))
    d.register()
    job = controller.enqueue(task())
    assert d.once()
    payload = stored_envelope(controller, job)
    assert payload["artifacts"] == [artifact, legacy]
    assert payload["hashes"] == {REPORT_PATH: artifact["sha256"], "legacy.log": "sha256:abc"}
    assert set(payload["engine_result"]) == {"status", "summary", "gate", "completed", "remaining", "evidence"}
    assert controller.status(job)["artifacts"][0]["inline_text"] == artifact["inline_text"]


def test_worker_keeps_artifacts_when_normalizing_gates(api):
    controller, url, root = api
    artifact = enriched()
    result = AdapterResult(status="DONE", summary="report", gate="invented-gate",
                           artifacts=[artifact], hashes={REPORT_PATH: artifact["sha256"]})
    d = worker(url, root, FixedAdapter(result))
    d.register()
    job = controller.enqueue(task())
    assert d.once()
    payload = stored_envelope(controller, job)
    assert payload["engine_result"]["gate"] is None
    assert payload["artifacts"] == [artifact]


def test_worker_fails_closed_on_invalid_artifacts_instead_of_looping(api):
    controller, url, root = api
    bad = enriched(path="../escape.md")
    result = AdapterResult(status="DONE", summary="report", artifacts=[bad], hashes={bad["path"]: bad["sha256"]})
    d = worker(url, root, FixedAdapter(result))
    d.register()
    job = controller.enqueue(task())
    assert d.once()
    status = controller.status(job)
    assert status["state"] == "FAILED"
    assert status["artifacts"] == [] and status["hashes"] == {}
    assert any("artifact-policy" in item for item in status["result"]["evidence"])
    assert d.pending_envelope is None
