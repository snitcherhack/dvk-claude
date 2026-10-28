from __future__ import annotations

import json
import os
import shlex
import urllib.error
import urllib.request
from typing import Any


DEFAULT_CONTROLLER_URL = "http://127.0.0.1:8787"
DEFAULT_ACTOR = "telegram-operator"
MIN_JOB_PREFIX = 8


class BridgeError(RuntimeError):
    def __init__(self, status: int | None, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _controller_url() -> str:
    return os.environ.get("HERMES_CONTROLLER_URL", DEFAULT_CONTROLLER_URL).rstrip("/")


def _operator_token() -> str:
    token = os.environ.get("HERMES_OPERATOR_TOKEN", "")
    if not token:
        raise BridgeError(None, "HERMES_OPERATOR_TOKEN is not configured")
    return token


def _actor() -> str:
    actor = os.environ.get("HERMES_GATE_ACTOR", DEFAULT_ACTOR).strip()
    return actor or DEFAULT_ACTOR


def _request(method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload, sort_keys=True).encode("utf-8")
    request = urllib.request.Request(
        _controller_url() + path,
        data=data,
        headers={
            "Authorization": f"Bearer {_operator_token()}",
            "Content-Type": "application/json",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            body = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        try:
            error_body = json.loads(exc.read())
            message = error_body.get("error") or f"HTTP {exc.code}"
        except (json.JSONDecodeError, AttributeError, UnicodeDecodeError):
            message = f"HTTP {exc.code}"
        raise BridgeError(exc.code, str(message)) from exc
    except urllib.error.URLError as exc:
        raise BridgeError(None, f"Controller unavailable: {exc.reason}") from exc

    if not isinstance(body, dict):
        raise BridgeError(None, "Controller returned a non-object response")
    return body


def pending_gates() -> list[dict[str, Any]]:
    payload = _request("GET", "/v1/gates/pending")
    gates = payload.get("gates")
    if not isinstance(gates, list):
        raise BridgeError(None, "Controller returned an invalid pending-gates response")
    required = {"job_id", "project", "gate", "source_run_id", "attempt", "summary"}
    for gate in gates:
        if not isinstance(gate, dict) or not required.issubset(gate):
            raise BridgeError(None, "Controller returned an invalid pending gate")
    return gates


def _gate_line(gate: dict[str, Any]) -> str:
    project = gate["project"] or "-"
    summary = " ".join(str(gate["summary"] or "").splitlines())
    return (
        f"- {project} | {gate['gate']} | job {gate['job_id'][:12]} | "
        f"intento {gate['attempt']}\n  {summary}"
    )


def handle_gates(raw_args: str = "") -> str:
    if raw_args.strip() not in {"", "list"}:
        return "Uso: /gates"
    try:
        gates = pending_gates()
    except BridgeError as exc:
        return f"⚠️ No se pudieron consultar los gates: {exc.message}"

    if not gates:
        return "No hay human gates pendientes."

    lines = [f"Human gates pendientes: {len(gates)}"]
    lines.extend(_gate_line(gate) for gate in gates)
    lines.append("Usa: /gate approve <job-prefix> [nota]  o  /gate reject <job-prefix> [nota]")
    return "\n".join(lines)


def _select_gate(job_prefix: str, gates: list[dict[str, Any]]) -> dict[str, Any]:
    if len(job_prefix) < MIN_JOB_PREFIX:
        raise BridgeError(None, f"job-prefix debe tener al menos {MIN_JOB_PREFIX} caracteres")
    matches = [gate for gate in gates if gate["job_id"].startswith(job_prefix)]
    if not matches:
        raise BridgeError(None, f"no hay gate pendiente para job {job_prefix}")
    if len(matches) > 1:
        raise BridgeError(None, f"job-prefix ambiguo: {job_prefix}")
    return matches[0]


def handle_gate(raw_args: str) -> str:
    try:
        parts = shlex.split(raw_args)
    except ValueError as exc:
        return f"Uso inválido: {exc}"

    if len(parts) < 2 or parts[0] not in {"approve", "reject"}:
        return "Uso: /gate approve|reject <job-prefix> [nota]"

    action, job_prefix = parts[0], parts[1]
    note = " ".join(parts[2:]).strip() or None

    try:
        gate = _select_gate(job_prefix, pending_gates())
        result = _request(
            "POST",
            f"/v1/gates/{action}",
            {
                "job_id": gate["job_id"],
                "gate": gate["gate"],
                "source_run_id": gate["source_run_id"],
                "actor": _actor(),
                "note": note,
            },
        )
    except BridgeError as exc:
        if exc.status == 409:
            return (
                "⚠️ El gate cambió o ya fue resuelto. "
                "No se aplicó ninguna decisión. Ejecuta /gates y revisa el estado actual."
            )
        if exc.status == 401:
            return "⛔ El bridge no está autorizado contra el Hermes Controller."
        return f"⚠️ No se pudo resolver el gate: {exc.message}"

    decision = result.get("decision", "-")
    disposition = result.get("disposition", "-")
    job_id = result.get("job_id", gate["job_id"])
    gate_name = result.get("gate", gate["gate"])

    if decision == "APPROVED":
        prefix = "✅ Gate aprobado"
    elif decision == "REJECTED":
        prefix = "❌ Gate rechazado"
    else:
        prefix = "Gate resuelto"

    return (
        f"{prefix}: {gate_name}\n"
        f"job: {job_id}\n"
        f"disposition: {disposition}"
    )
