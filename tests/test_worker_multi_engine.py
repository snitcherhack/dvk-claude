from __future__ import annotations

import copy
import hashlib
import json
import threading
from pathlib import Path

import pytest

from hermes_controller.adapters import (
    AdapterResult,
    ClaudeAgentAdapter,
    CodexRunAdapter,
    EngineRoutingAdapter,
    HybridAdapter,
    MockAdapter,
)
from hermes_controller.api import serve
from hermes_controller.brainstorm import BrainstormAdapter
from hermes_controller.brainstorm_contract import build_brainstorm_config
from hermes_controller.clock import FakeClock
from hermes_controller.controller import Controller, ControllerError
from hermes_controller.worker import WorkerDaemon

REPO = Path(__file__).parents[1]
MAIN_LINUX = REPO / "config" / "hermes-workers" / "main-linux.json"
RUNTIME_SCHEMA = REPO / "hermes_controller" / "schemas" / "hermes-worker-runtime.schema.json"
WORKER = {"worker_id": "main-linux", "platform": "linux", "environment": "WSL",
          "capabilities": ["codex", "claude", "git"], "max_concurrent_jobs": 1}
REPORT_TEXT = "# Hermes brainstorm report\n\nDecision: RECOMMENDED_FOR_PILOT\n"


def report_artifact() -> dict:
    data = REPORT_TEXT.encode("utf-8")
    return {"path": "orchestration/brainstorm-report.md", "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data), "media_type": "text/markdown", "inline_text": REPORT_TEXT}


def brainstorm_task(**extra) -> dict:
    return {
        "brain": {"repository": "git@example/brain.git", "ref": "main", "commit": "a" * 40, "task_file": "task.md"},
        "project": "cafe", "repository": "git@example/cafe.git", "ref": "main", "task_type": "brainstorm",
        "platform": "linux", "required_capabilities": ["claude", "codex"], "execution_profile": "brainstorm",
        "execution_engine": "brainstorm", "human_gates": [], "idempotency_policy": "safe_retry",
        "brainstorm": build_brainstorm_config(), **extra,
    }


def legacy_task(**extra) -> dict:
    return {
        "brain": {"repository": "git@example/brain.git", "ref": "main", "commit": "a" * 40, "task_file": "task.md"},
        "project": "cafe", "repository": "git@example/cafe.git", "ref": "main", "task_type": "development",
        "platform": "linux", "required_capabilities": ["codex"], "execution_profile": "hermes",
        "human_gates": [], "idempotency_policy": "safe_retry", **extra,
    }


class Recorder:
    """Adapter double that records the prepared task it receives."""

    def __init__(self, result: AdapterResult | None = None) -> None:
        self.tasks: list[dict] = []
        self.result = result or AdapterResult(status="DONE", summary="ok")

    def execute(self, task):
        self.tasks.append(copy.deepcopy(task))
        return self.result


@pytest.fixture
def api(tmp_path):
    controller = Controller(tmp_path / "controller", clock=FakeClock(1_000))
    server = serve(controller, port=0, enrollment_tokens={"main-linux": "worker-secret-token"})
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield controller, f"http://127.0.0.1:{server.server_port}", tmp_path
    server.shutdown()
    server.server_close()
    controller.close()


def daemon(url, root, adapter) -> WorkerDaemon:
    return WorkerDaemon({"controller_url": url, "token": "worker-secret-token", "state_file": str(root / "state.json"),
                         "heartbeat_interval_ms": 10, "retry_backoff_ms": 1, "max_backoff_ms": 5, "worker": WORKER},
                        adapter)


def stored_envelope(controller, job_id) -> dict:
    return json.loads(controller.db.execute("SELECT result_json FROM runs WHERE job_id=?", (job_id,)).fetchone()["result_json"])


# --- runtime context --------------------------------------------------------------------------------------

def test_worker_injects_only_job_run_and_attempt(api):
    controller, url, root = api
    brainstorm = Recorder()
    worker = daemon(url, root, EngineRoutingAdapter({"codex": Recorder(), "brainstorm": brainstorm}))
    worker.register()
    job_id = controller.enqueue(brainstorm_task())
    assert worker.once()
    task = brainstorm.tasks[0]
    run = controller.status(job_id)["runs"][0]
    assert task["_hermes_runtime"] == {"job_id": job_id, "run_id": run["run_id"], "attempt": 1}
    serialized = json.dumps(task)
    lease = controller.db.execute("SELECT lease_id, lease_token FROM runs WHERE job_id=?", (job_id,)).fetchone()
    assert lease["lease_id"] not in serialized and lease["lease_token"] not in serialized
    assert "worker-secret-token" not in serialized
    assert "lease" not in serialized.lower()


def test_controller_rejects_forged_runtime_in_task_snapshot(api):
    controller, _url, _root = api
    with pytest.raises(ControllerError, match="reserved"):
        controller.enqueue(brainstorm_task(_hermes_runtime={
            "job_id": "evil", "run_id": "evil", "attempt": 99, "lease_token": "x",
        }))


def test_claim_task_is_not_mutated(api):
    controller, url, root = api
    worker = daemon(url, root, EngineRoutingAdapter({"codex": Recorder(), "brainstorm": Recorder()}))
    claim = {"job_id": "j", "run_id": "r", "attempt": 3, "lease_id": "l", "lease_token": "t", "task": brainstorm_task()}
    original = copy.deepcopy(claim)
    prepared = worker._prepare_task(claim)
    assert claim == original
    assert "_hermes_runtime" not in claim["task"]
    assert prepared["_hermes_runtime"] == {"job_id": "j", "run_id": "r", "attempt": 3}
    assert prepared is not claim["task"]
    prepared["brain"]["commit"] = "changed"
    assert claim["task"]["brain"]["commit"] == "a" * 40


@pytest.mark.parametrize("bad", [{"attempt": 0}, {"attempt": True}, {"run_id": ""}, {"job_id": None}])
def test_invalid_claim_identity_blocks_preparation(api, bad):
    _controller, url, root = api
    worker = daemon(url, root, Recorder())
    claim = {"job_id": "j", "run_id": "r", "attempt": 1, "lease_id": "l", "lease_token": "t", "task": brainstorm_task()}
    claim.update(bad)
    with pytest.raises(ValueError, match="claim"):
        worker._prepare_task(claim)


def test_restart_with_persisted_claim_reproduces_the_same_runtime(api):
    controller, url, root = api
    worker = daemon(url, root, Recorder())
    worker.register()
    job_id = controller.enqueue(brainstorm_task())
    claim = controller.claim("main-linux")
    worker._save_state(claim)
    brainstorm = Recorder()
    restarted = daemon(url, root, EngineRoutingAdapter({"codex": Recorder(), "brainstorm": brainstorm}))
    assert restarted.active_claim == claim
    assert restarted.once()
    assert brainstorm.tasks[0]["_hermes_runtime"] == {"job_id": job_id, "run_id": claim["run_id"], "attempt": claim["attempt"]}
    assert controller.status(job_id)["state"] == "DONE"


def test_new_controller_claim_changes_run_and_attempt(tmp_path):
    clock = FakeClock(1_000)
    controller = Controller(tmp_path / "controller", clock=clock)
    controller.register_worker(WORKER)
    controller.enqueue(brainstorm_task())
    first = controller.claim("main-linux")
    clock.advance(10_000_000)
    controller.reconcile_expired_leases()
    second = controller.claim("main-linux")
    controller.close()
    worker = WorkerDaemon({"controller_url": "http://127.0.0.1:9", "token": "t", "state_file": str(tmp_path / "s.json"),
                           "worker": WORKER}, Recorder())
    one, two = worker._prepare_task(first)["_hermes_runtime"], worker._prepare_task(second)["_hermes_runtime"]
    assert one["job_id"] == two["job_id"]
    assert (one["attempt"], two["attempt"]) == (1, 2)
    assert one["run_id"] != two["run_id"]


# --- routing and envelope ----------------------------------------------------------------------------------

@pytest.mark.parametrize("task, expected", [
    (brainstorm_task(), "brainstorm"),
    (legacy_task(execution_engine="codex"), "codex"),
    (legacy_task(execution_engine="claude", required_capabilities=["claude"]), "claude"),
    (legacy_task(execution_engine="hybrid", required_capabilities=["claude", "codex"]), "hybrid"),
    (legacy_task(execution_engine="native", required_capabilities=["final_render"]), "native"),
    (legacy_task(), "codex"),
    (legacy_task(required_capabilities=["final_render"]), "native"),
])
def test_execution_engine_for_task(tmp_path, task, expected):
    worker = WorkerDaemon({"controller_url": "http://127.0.0.1:9", "token": "t", "state_file": str(tmp_path / "s.json"),
                           "worker": WORKER}, Recorder())
    assert worker._execution_engine_for_task(task) == expected


def test_brainstorm_envelope_carries_engine_artifacts_and_hashes(api):
    controller, url, root = api
    artifact = report_artifact()
    legacy = {"path": "orchestration/ranking.json"}
    result = AdapterResult(status="DONE", summary="Brainstorm RECOMMENDED_FOR_PILOT: winner C01 \"A\" with HIGH confidence",
                           completed=["prepare", "report"], remaining=["Building the pilot requires a new task."],
                           evidence=["/x/brainstorm-report.json"], artifacts=[artifact, legacy],
                           hashes={artifact["path"]: artifact["sha256"], legacy["path"]: "sha256:legacy"})
    brainstorm, codex = Recorder(result), Recorder()
    worker = daemon(url, root, EngineRoutingAdapter({"codex": codex, "brainstorm": brainstorm}))
    worker.register()
    job_id = controller.enqueue(brainstorm_task())
    assert worker.once()
    assert codex.tasks == [] and len(brainstorm.tasks) == 1
    envelope = stored_envelope(controller, job_id)
    assert envelope["engine"] == "brainstorm"
    assert "codex_result" not in envelope
    assert set(envelope["engine_result"]) == {"status", "summary", "gate", "completed", "remaining", "evidence"}
    assert envelope["engine_result"]["status"] == "DONE" and envelope["engine_result"]["gate"] is None
    assert envelope["artifacts"] == [artifact, legacy]
    assert envelope["hashes"] == {artifact["path"]: artifact["sha256"], legacy["path"]: "sha256:legacy"}
    status = controller.status(job_id)
    assert status["engine"] == "brainstorm" and status["state"] == "DONE"
    assert status["artifacts"][0]["inline_text"] == REPORT_TEXT


def test_legacy_task_without_engine_keeps_codex_route(api):
    controller, url, root = api
    codex = Recorder()
    worker = daemon(url, root, EngineRoutingAdapter({"codex": codex, "brainstorm": Recorder()}))
    worker.register()
    job_id = controller.enqueue(legacy_task())
    assert worker.once()
    assert len(codex.tasks) == 1
    assert stored_envelope(controller, job_id)["engine"] == "codex"


# --- multi-engine construction --------------------------------------------------------------------------------

@pytest.fixture
def engine_env(tmp_path, monkeypatch):
    runner = tmp_path / "hermes-codex-run.sh"
    runner.write_text("#!/bin/sh\n", encoding="utf-8")
    cli = tmp_path / "claude"
    cli.write_text("#!/bin/sh\n", encoding="utf-8")
    roots = tmp_path / "roots"
    roots.mkdir()
    monkeypatch.setenv("HERMES_CODEX_RUN", str(runner))
    monkeypatch.setenv("HERMES_AUTHORIZED_ROOTS", str(roots))
    monkeypatch.setenv("HERMES_CLAUDE_CLI", str(cli))
    monkeypatch.setenv("HERMES_TASK_ROOTS", str(roots))
    monkeypatch.setenv("HERMES_MAIN_LINUX_TOKEN", "fake-token-for-tests")
    return tmp_path


def multi_config(**engines) -> dict:
    base = {
        "codex": {"kind": "codex-run", "runner_path_env": "HERMES_CODEX_RUN", "authorized_roots_env": "HERMES_AUTHORIZED_ROOTS",
                  "execution_profiles": ["hermes", "review", "brainstorm"], "task_types": ["development", "brainstorm"]},
        "claude": {"kind": "claude-agent", "authorized_roots_env": "HERMES_AUTHORIZED_ROOTS", "cli_path_env": "HERMES_CLAUDE_CLI",
                   "execution_profiles": ["claude_smoke", "hermes", "brainstorm"],
                   "task_types": ["claude_smoke", "development", "brainstorm"], "subscription_only": True},
        "hybrid": {"kind": "hybrid", "primary_engine": "claude", "review_engine": "codex", "max_review_rounds": 1},
        "brainstorm": {"kind": "brainstorm", "claude_engine": "claude", "codex_engine": "codex"},
    }
    base.update(engines)
    return {"adapters": {name: spec for name, spec in base.items() if spec is not None}, "default_execution_engine": "codex"}


def test_multi_engine_construction_wires_brainstorm_to_the_built_engines(engine_env):
    router = WorkerDaemon._adapter_from_worker_config(multi_config())
    assert isinstance(router, EngineRoutingAdapter)
    assert set(router.adapters) == {"codex", "claude", "hybrid", "brainstorm"}
    codex, claude = router.adapters["codex"], router.adapters["claude"]
    assert isinstance(codex, CodexRunAdapter) and isinstance(claude, ClaudeAgentAdapter)
    assert isinstance(router.adapters["hybrid"], HybridAdapter)
    assert router.adapters["hybrid"].primary is claude and router.adapters["hybrid"].reviewer is codex
    brainstorm = router.adapters["brainstorm"]
    assert isinstance(brainstorm, BrainstormAdapter)
    assert brainstorm.engines["claude"] is claude and brainstorm.engines["codex"] is codex
    assert router.default_engine == "codex"


@pytest.mark.parametrize("engines, message", [
    ({"claude": None, "hybrid": None}, "claude"),
    ({"codex": None, "hybrid": None}, "codex"),
    ({"brainstorm": {"kind": "brainstorm", "claude_engine": "missing", "codex_engine": "codex"}}, "missing"),
    ({"brainstorm": {"kind": "brainstorm", "codex_engine": "codex"}}, "claude_engine"),
    ({"brainstorm": {"kind": "brainstorm", "claude_engine": "codex", "codex_engine": "codex"}}, "claude_engine"),
    ({"brainstorm": {"kind": "brainstorm", "claude_engine": "claude", "codex_engine": "hybrid"}}, "codex_engine"),
    ({"brainstorm": {"kind": "brainstorm", "claude_engine": "claude", "codex_engine": "codex", "extra": 1}}, "fields"),
    ({"native": {"kind": "brainstorm", "claude_engine": "claude", "codex_engine": "codex"}}, "brainstorm"),
    ({"brainstorm": {"kind": "hybrid", "primary_engine": "claude", "review_engine": "codex"}}, "brainstorm"),
])
def test_invalid_brainstorm_configuration_fails_closed(engine_env, engines, message):
    with pytest.raises(ValueError, match=message):
        WorkerDaemon._adapter_from_worker_config(multi_config(**engines))


def test_brainstorm_references_must_support_structured_execution(engine_env, monkeypatch):
    monkeypatch.setattr(WorkerDaemon, "_adapter_from_config",
                        staticmethod(lambda spec: MockAdapter() if spec.get("kind") == "claude-agent"
                                     else CodexRunAdapter(runner_path="/bin/true", authorized_roots=["/tmp"])))
    with pytest.raises(ValueError, match="execute_structured"):
        WorkerDaemon._adapter_from_worker_config(multi_config(hybrid=None))


def test_brainstorm_cannot_be_the_default_engine(engine_env):
    config = multi_config()
    config["default_execution_engine"] = "brainstorm"
    with pytest.raises(ValueError, match="explicit-only"):
        WorkerDaemon._adapter_from_worker_config(config)


def test_standalone_brainstorm_adapter_is_rejected():
    with pytest.raises(ValueError, match="adapters"):
        WorkerDaemon._adapter_from_worker_config({"adapter": {"kind": "brainstorm", "claude_engine": "claude",
                                                              "codex_engine": "codex"}})


# --- versioned config and runtime schema ------------------------------------------------------------------------

def test_runtime_schema_admits_brainstorm_but_not_as_default():
    schema = json.loads(RUNTIME_SCHEMA.read_text(encoding="utf-8"))
    adapter = schema["$defs"]["adapter"]["properties"]
    assert "brainstorm" in adapter["kind"]["enum"]
    assert adapter["claude_engine"] == {"type": "string", "minLength": 1}
    assert adapter["codex_engine"] == {"type": "string", "minLength": 1}
    assert "brainstorm" in schema["properties"]["adapters"]["propertyNames"]["enum"]
    assert "brainstorm" not in schema["properties"]["default_execution_engine"]["enum"]
    assert set(schema["properties"]["default_execution_engine"]["enum"]) == {"codex", "claude", "hybrid", "native"}


def test_main_linux_config_is_multi_engine_without_secrets():
    text = MAIN_LINUX.read_text(encoding="utf-8")
    config = json.loads(text)
    schema = json.loads(RUNTIME_SCHEMA.read_text(encoding="utf-8"))
    assert "adapter" not in config
    assert set(config) <= set(schema["properties"])
    assert set(config["adapters"]) == {"codex", "claude", "hybrid", "brainstorm"}
    for spec in config["adapters"].values():
        assert set(spec) <= set(schema["$defs"]["adapter"]["properties"])
    assert config["default_execution_engine"] == "codex"
    assert config["adapters"]["brainstorm"] == {"kind": "brainstorm", "claude_engine": "claude", "codex_engine": "codex"}
    assert config["adapters"]["hybrid"] == {"kind": "hybrid", "primary_engine": "claude", "review_engine": "codex",
                                            "max_review_rounds": 1}
    capabilities = config["worker"]["capabilities"]
    assert {"codex", "claude"} <= set(capabilities) and "brainstorm" not in capabilities
    assert config["token_env"] == "HERMES_MAIN_LINUX_TOKEN" and "token" not in config

    def strings(value):
        if isinstance(value, dict):
            for item in value.values():
                yield from strings(item)
        elif isinstance(value, list):
            for item in value:
                yield from strings(item)
        elif isinstance(value, str):
            yield value

    for value in strings(config):
        assert "/home/" not in value and "sk-" not in value and "Bearer" not in value
        assert len(value) < 80


def test_main_linux_config_builds_all_engines_with_fake_environment(engine_env):
    daemon_from_file = WorkerDaemon.from_file(MAIN_LINUX)
    router = daemon_from_file.adapter
    assert set(router.adapters) == {"codex", "claude", "hybrid", "brainstorm"}
    assert router.adapters["brainstorm"].engines == {"claude": router.adapters["claude"], "codex": router.adapters["codex"]}
    assert "brainstorm" in router.adapters["codex"].profiles and "brainstorm" in router.adapters["codex"].task_types
    assert "brainstorm" in router.adapters["claude"].profiles and "brainstorm" in router.adapters["claude"].task_types
    assert router.adapters["claude"].subscription_only is True
    assert daemon_from_file.task_materialization_roots
