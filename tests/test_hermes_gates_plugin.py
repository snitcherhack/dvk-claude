import importlib.util
import sys
from pathlib import Path

import pytest


PLUGIN_DIR = Path(__file__).parents[1] / "hermes_agent_plugins" / "hermes-gates"


def _load_module(name, path, *, package=False):
    spec = importlib.util.spec_from_file_location(
        name,
        path,
        submodule_search_locations=[str(path.parent)] if package else None,
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(name, None)
        raise
    return module


@pytest.fixture
def bridge():
    name = "hermes_gates_bridge_test"
    module = _load_module(name, PLUGIN_DIR / "bridge.py")
    yield module
    sys.modules.pop(name, None)


PENDING = {
    "job_id": "12345678-abcd-4000-9000-1234567890ab",
    "project": "deividk-ansible-packs",
    "gate": "INFRASTRUCTURE_APPLY_APPROVAL_REQUIRED",
    "source_run_id": "run-1111",
    "attempt": 2,
    "summary": "Apply requires approval",
}


def test_gates_lists_pending_without_source_run_id(monkeypatch, bridge):
    monkeypatch.setattr(
        bridge,
        "_request",
        lambda method, path, payload=None: {"gates": [PENDING]},
    )

    text = bridge.handle_gates("")

    assert "Human gates pendientes: 1" in text
    assert "deividk-ansible-packs" in text
    assert "12345678-abc" in text
    assert "run-1111" not in text


def test_gate_approve_refetches_and_posts_exact_contract(monkeypatch, bridge):
    calls = []

    def fake_request(method, path, payload=None):
        calls.append((method, path, payload))
        if method == "GET":
            return {"gates": [PENDING]}
        return {
            "job_id": PENDING["job_id"],
            "gate": PENDING["gate"],
            "decision": "APPROVED",
            "source_run_id": PENDING["source_run_id"],
            "disposition": "QUEUED",
        }

    monkeypatch.setattr(bridge, "_request", fake_request)
    monkeypatch.setenv("HERMES_GATE_ACTOR", "deiv")

    text = bridge.handle_gate('approve 12345678 "revisado en Telegram"')

    assert calls[0] == ("GET", "/v1/gates/pending", None)
    assert calls[1] == (
        "POST",
        "/v1/gates/approve",
        {
            "job_id": PENDING["job_id"],
            "gate": PENDING["gate"],
            "source_run_id": PENDING["source_run_id"],
            "actor": "deiv",
            "note": "revisado en Telegram",
        },
    )
    assert "Gate aprobado" in text
    assert "QUEUED" in text


def test_gate_reject_uses_current_pending_gate(monkeypatch, bridge):
    calls = []

    def fake_request(method, path, payload=None):
        calls.append((method, path, payload))
        if method == "GET":
            return {"gates": [PENDING]}
        return {
            "job_id": PENDING["job_id"],
            "gate": PENDING["gate"],
            "decision": "REJECTED",
            "source_run_id": PENDING["source_run_id"],
            "disposition": "CANCELLED",
        }

    monkeypatch.setattr(bridge, "_request", fake_request)

    text = bridge.handle_gate("reject 12345678 no aplicar")

    assert calls[1][1] == "/v1/gates/reject"
    assert calls[1][2]["source_run_id"] == "run-1111"
    assert "Gate rechazado" in text
    assert "CANCELLED" in text


def test_gate_requires_unique_long_enough_job_prefix(monkeypatch, bridge):
    second = {**PENDING, "job_id": "12345678-ffff-4000-9000-1234567890ab"}
    monkeypatch.setattr(
        bridge,
        "_request",
        lambda method, path, payload=None: {"gates": [PENDING, second]},
    )

    assert "al menos 8" in bridge.handle_gate("approve 1234")
    assert "ambiguo" in bridge.handle_gate("approve 12345678")


def test_gate_maps_409_to_safe_refresh_message(monkeypatch, bridge):
    def fake_request(method, path, payload=None):
        if method == "GET":
            return {"gates": [PENDING]}
        raise bridge.BridgeError(
            409,
            "source_run_id does not match current waiting run",
        )

    monkeypatch.setattr(bridge, "_request", fake_request)

    text = bridge.handle_gate("approve 12345678-abcd")

    assert "No se aplicó ninguna decisión" in text
    assert "/gates" in text


def test_pending_response_validation(monkeypatch, bridge):
    monkeypatch.setattr(
        bridge,
        "_request",
        lambda method, path, payload=None: {"gates": [{}]},
    )

    with pytest.raises(bridge.BridgeError, match="invalid pending gate"):
        bridge.pending_gates()


def test_register_exposes_only_gate_commands():
    package_name = "hermes_gates_plugin_test"
    module = _load_module(package_name, PLUGIN_DIR / "__init__.py", package=True)
    registered = []

    class FakeCtx:
        def register_command(self, name, handler, description=""):
            registered.append((name, handler, description))

    try:
        module.register(FakeCtx())
    finally:
        sys.modules.pop(package_name, None)
        sys.modules.pop(f"{package_name}.bridge", None)

    assert [item[0] for item in registered] == ["gates", "gate"]
