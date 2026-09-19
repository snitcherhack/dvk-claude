"""Durable local job, lease and result state machine for Hermes."""

from __future__ import annotations

import json
import secrets
import sqlite3
import uuid
import hashlib
import hmac
import re
from pathlib import Path
from typing import Any

from .clock import MonotonicClock
from .engine_policy import select_engine

LEASE_DURATION_MS = 180_000
WORKER_OFFLINE_MS = 90_000
RESULT_STATUSES = {"DONE", "WAIT_USER", "BLOCKED", "FAILED"}
ENGINE_CAPABILITIES = {"codex": {"codex"}, "claude": {"claude"}, "hybrid": {"codex", "claude"}, "native": set()}


class ControllerError(RuntimeError):
    pass


class StaleResultError(ControllerError):
    pass


class Controller:
    """Local-only Controller. All state belongs under the caller's runtime root."""

    def __init__(self, runtime_root: str | Path, *, clock: Any | None = None) -> None:
        self.root = Path(runtime_root)
        for name in ("runs", "artifacts", "locks", "logs"):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        self.clock = clock or MonotonicClock()
        self.db = sqlite3.connect(self.root / "controller.db", isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def close(self) -> None:
        self.db.close()

    def _create_schema(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS workers (
              worker_id TEXT PRIMARY KEY, descriptor_json TEXT NOT NULL,
              registered_at INTEGER NOT NULL, last_heartbeat_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS worker_credentials (
              worker_id TEXT PRIMARY KEY, token_hash TEXT NOT NULL,
              created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS jobs (
              job_id TEXT PRIMARY KEY, task_json TEXT NOT NULL,
              state TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 0,
              active_run_id TEXT, idempotency_key TEXT UNIQUE,
              idempotent INTEGER NOT NULL, dependency_job_id TEXT,
              created_at INTEGER NOT NULL,
              FOREIGN KEY(dependency_job_id) REFERENCES jobs(job_id)
            );
            CREATE TABLE IF NOT EXISTS runs (
              run_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, worker_id TEXT NOT NULL,
              attempt INTEGER NOT NULL, lease_id TEXT NOT NULL, lease_token TEXT NOT NULL,
              state TEXT NOT NULL, claimed_at INTEGER NOT NULL,
              lease_expires_at INTEGER NOT NULL, result_json TEXT,
              FOREIGN KEY(job_id) REFERENCES jobs(job_id),
              FOREIGN KEY(worker_id) REFERENCES workers(worker_id)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_attempt_per_job
              ON runs(job_id, attempt);
            CREATE TABLE IF NOT EXISTS events (
              event_id INTEGER PRIMARY KEY AUTOINCREMENT, occurred_at INTEGER NOT NULL,
              entity_type TEXT NOT NULL, entity_id TEXT NOT NULL,
              event_type TEXT NOT NULL, payload_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS projects (
              project_id TEXT PRIMARY KEY, manifest_json TEXT NOT NULL,
              registered_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
            );
            """
        )

    def _event(self, entity_type: str, entity_id: str, event_type: str, **payload: Any) -> None:
        self.db.execute(
            "INSERT INTO events VALUES (NULL, ?, ?, ?, ?, ?)",
            (self.clock.now(), entity_type, entity_id, event_type, json.dumps(payload, sort_keys=True)),
        )

    def register_worker(self, worker: dict[str, Any]) -> str:
        self._validate_worker(worker)
        now = self.clock.now()
        self.db.execute(
            "INSERT INTO workers VALUES (?, ?, ?, ?) ON CONFLICT(worker_id) DO UPDATE SET descriptor_json=excluded.descriptor_json, last_heartbeat_at=excluded.last_heartbeat_at",
            (worker["worker_id"], json.dumps(worker, sort_keys=True), now, now),
        )
        self._event("worker", worker["worker_id"], "WORKER_REGISTERED")
        return worker["worker_id"]

    def provision_worker_token(self, worker_id: str, token: str) -> None:
        """Store only a PBKDF2 derivation for a worker credential."""
        if not worker_id or not token:
            raise ControllerError("worker_id and token are required")
        digest = self._hash_token(token)
        self.db.execute(
            "INSERT INTO worker_credentials VALUES (?, ?, ?) ON CONFLICT(worker_id) DO UPDATE SET token_hash=excluded.token_hash, created_at=excluded.created_at",
            (worker_id, digest, self.clock.now()),
        )

    def authenticate_worker(self, worker_id: str, token: str) -> bool:
        row = self.db.execute("SELECT token_hash FROM worker_credentials WHERE worker_id=?", (worker_id,)).fetchone()
        return bool(row and hmac.compare_digest(row["token_hash"], self._hash_token(token, row["token_hash"])))

    def heartbeat_worker(self, worker_id: str) -> None:
        cursor = self.db.execute("UPDATE workers SET last_heartbeat_at=? WHERE worker_id=?", (self.clock.now(), worker_id))
        if not cursor.rowcount:
            raise ControllerError(f"unknown worker: {worker_id}")
        self._event("worker", worker_id, "WORKER_HEARTBEAT")

    def worker_status(self, worker_id: str) -> dict[str, Any]:
        row = self.db.execute("SELECT last_heartbeat_at FROM workers WHERE worker_id=?", (worker_id,)).fetchone()
        if row is None:
            raise ControllerError(f"unknown worker: {worker_id}")
        age = self.clock.now() - row["last_heartbeat_at"]
        return {"worker_id": worker_id, "state": "OFFLINE" if age > WORKER_OFFLINE_MS else "ONLINE", "heartbeat_age_ms": age}

    def register_project(self, manifest: dict[str, Any]) -> str:
        self._validate_project(manifest)
        project_id = manifest["project_id"]
        now = self.clock.now()
        payload = json.dumps(manifest, sort_keys=True)
        self.db.execute(
            """INSERT INTO projects VALUES (?, ?, ?, ?)
               ON CONFLICT(project_id) DO UPDATE SET
                 manifest_json=excluded.manifest_json,
                 updated_at=excluded.updated_at""",
            (project_id, payload, now, now),
        )
        self._event("project", project_id, "PROJECT_REGISTERED")
        return project_id

    def list_projects(self) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT project_id, manifest_json, registered_at, updated_at FROM projects ORDER BY project_id"
        ).fetchall()
        return [
            {
                "project_id": row["project_id"],
                "manifest": json.loads(row["manifest_json"]),
                "registered_at": row["registered_at"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    def project(self, project_id: str) -> dict[str, Any]:
        row = self.db.execute(
            "SELECT project_id, manifest_json, registered_at, updated_at FROM projects WHERE project_id=?",
            (project_id,),
        ).fetchone()
        if row is None:
            raise ControllerError(f"unknown project: {project_id}")
        return {
            "project_id": row["project_id"],
            "manifest": json.loads(row["manifest_json"]),
            "registered_at": row["registered_at"],
            "updated_at": row["updated_at"],
        }

    def remove_project(self, project_id: str) -> None:
        cursor = self.db.execute("DELETE FROM projects WHERE project_id=?", (project_id,))
        if not cursor.rowcount:
            raise ControllerError(f"unknown project: {project_id}")
        self._event("project", project_id, "PROJECT_REMOVED")

    def build_project_task(
        self,
        project_id: str,
        instruction: str,
        *,
        engine: str | None = None,
        idempotency_key: str | None = None,
        workspaces: list[str] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(instruction, str) or not instruction.strip():
            raise ControllerError("project task instruction is required")
        if len(instruction.encode("utf-8")) > 1_048_576:
            raise ControllerError("project task instruction exceeds 1 MiB")

        manifest = self.project(project_id)["manifest"]
        requested_engine = engine or manifest["default_engine"]
        if requested_engine == "auto":
            policy_name = manifest.get("engine_policy", "balanced-v1")
            try:
                decision = select_engine(instruction, manifest["allowed_engines"], policy=policy_name)
            except ValueError as exc:
                raise ControllerError(str(exc)) from exc
            selected_engine = decision.engine
            selection = {
                "mode": "auto",
                "policy": decision.policy,
                "rule": decision.rule,
                "selected_engine": decision.engine,
            }
        else:
            selected_engine = requested_engine
            if selected_engine not in manifest["allowed_engines"]:
                raise ControllerError(f"engine is not allowed for project: {selected_engine}")
            selection = {
                "mode": "explicit" if engine is not None else "project-default",
                "selected_engine": selected_engine,
            }

        requested_workspaces = list(dict.fromkeys(workspaces or []))
        declared_workspaces = manifest.get("workspaces", {})
        selected_workspaces: dict[str, str] = {}
        for name in requested_workspaces:
            if name not in declared_workspaces:
                raise ControllerError(f"unknown project workspace: {name}")
            selected_workspaces[name] = declared_workspaces[name]

        job_id = str(uuid.uuid4())
        runtime_directory = manifest["runtime_directory"].rstrip("/\\")
        run_output_dir = f"{runtime_directory}/{job_id}"
        capabilities = set(manifest.get("capabilities", []))
        capabilities.update(ENGINE_CAPABILITIES[selected_engine])

        task: dict[str, Any] = {
            "job_id": job_id,
            "brain": {
                "repository": f"inline://hermes-projects/{project_id}",
                "ref": project_id,
                "commit": hashlib.sha256(instruction.encode("utf-8")).hexdigest(),
            },
            "task_text": instruction,
            "project": project_id,
            "repository": manifest["repository"],
            "ref": manifest["ref"],
            "task_type": manifest.get("task_type", "development"),
            "platform": manifest["platform"],
            "required_capabilities": sorted(capabilities),
            "execution_profile": manifest.get("execution_profile", "hermes"),
            "execution_engine": selected_engine,
            "engine_selection": selection,
            "human_gates": list(manifest.get("human_gates", [])),
            "idempotency_policy": manifest.get("idempotency_policy", "safe_retry"),
            "working_directory": manifest["working_directory"],
            "run_output_dir": run_output_dir,
            "allowed_paths": list(dict.fromkeys([
                manifest["working_directory"],
                runtime_directory,
                *manifest.get("allowed_paths", []),
                *selected_workspaces.values(),
            ])),
            "selected_workspaces": selected_workspaces,
            "project_manifest_version": 1,
        }
        if manifest.get("worker_id"):
            task["worker_id"] = manifest["worker_id"]
        if manifest.get("timeout_seconds") is not None:
            task["timeout_seconds"] = manifest["timeout_seconds"]
        if manifest.get("max_turns") is not None:
            task["max_turns"] = manifest["max_turns"]
        if idempotency_key:
            task["idempotency_key"] = idempotency_key
        self._validate_task(task)
        return task

    def enqueue(self, task: dict[str, Any]) -> str:
        self._validate_task(task)
        key = task.get("idempotency_key")
        if key:
            row = self.db.execute("SELECT job_id FROM jobs WHERE idempotency_key=?", (key,)).fetchone()
            if row:
                return row["job_id"]
        job_id = task.get("job_id") or str(uuid.uuid4())
        policy = task["idempotency_policy"]
        self.db.execute(
            "INSERT INTO jobs VALUES (?, ?, 'QUEUED', 0, NULL, ?, ?, ?, ?)",
            (job_id, json.dumps(task, sort_keys=True), key, int(policy == "safe_retry"), task.get("depends_on"), self.clock.now()),
        )
        self._event(
            "job", job_id, "JOB_ENQUEUED",
            task_type=task["task_type"],
            project=task["project"],
            engine=self._task_engine(task),
            engine_selection=task.get("engine_selection"),
        )
        return job_id

    def claim(self, worker_id: str) -> dict[str, Any] | None:
        now = self.clock.now()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            worker = self.db.execute("SELECT descriptor_json FROM workers WHERE worker_id=?", (worker_id,)).fetchone()
            if worker is None:
                raise ControllerError(f"unknown worker: {worker_id}")
            # A claim is also an authenticated liveness signal in this local API.
            self.db.execute("UPDATE workers SET last_heartbeat_at=? WHERE worker_id=?", (now, worker_id))
            descriptor = json.loads(worker["descriptor_json"])
            active = self.db.execute(
                "SELECT COUNT(*) AS n FROM runs WHERE worker_id=? AND state='RUNNING' AND lease_expires_at>=?",
                (worker_id, now),
            ).fetchone()["n"]
            if active >= descriptor["max_concurrent_jobs"]:
                self.db.execute("COMMIT")
                return None
            rows = self.db.execute(
                """SELECT j.* FROM jobs j LEFT JOIN jobs parent ON parent.job_id=j.dependency_job_id
                   WHERE j.state='QUEUED' AND (j.dependency_job_id IS NULL OR parent.state='DONE')
                   ORDER BY j.created_at, j.job_id"""
            ).fetchall()
            for job in rows:
                task = json.loads(job["task_json"])
                if not self._worker_matches(descriptor, task):
                    continue
                attempt = job["attempt"] + 1
                run_id, lease_id = str(uuid.uuid4()), str(uuid.uuid4())
                token = secrets.token_urlsafe(32)
                self.db.execute("UPDATE jobs SET state='RUNNING', attempt=?, active_run_id=? WHERE job_id=? AND state='QUEUED'", (attempt, run_id, job["job_id"]))
                self.db.execute(
                    "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, 'RUNNING', ?, ?, NULL)",
                    (run_id, job["job_id"], worker_id, attempt, lease_id, token, now, now + LEASE_DURATION_MS),
                )
                self._event("run", run_id, "RUN_CLAIMED", job_id=job["job_id"], attempt=attempt, worker_id=worker_id)
                self.db.execute("COMMIT")
                return {"job_id": job["job_id"], "run_id": run_id, "attempt": attempt, "lease_id": lease_id, "lease_token": token, "task": task}
            self.db.execute("COMMIT")
            return None
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def heartbeat_run(self, run_id: str, lease_id: str, lease_token: str) -> None:
        now = self.clock.now()
        cursor = self.db.execute(
            "UPDATE runs SET lease_expires_at=? WHERE run_id=? AND lease_id=? AND lease_token=? AND state='RUNNING' AND lease_expires_at>=?",
            (now + LEASE_DURATION_MS, run_id, lease_id, lease_token, now),
        )
        if not cursor.rowcount:
            raise StaleResultError("lease is not active")
        self._event("run", run_id, "LEASE_RENEWED")

    def reconcile_expired_leases(self) -> list[str]:
        now = self.clock.now()
        expired = self.db.execute("SELECT * FROM runs WHERE state='RUNNING' AND lease_expires_at < ?", (now,)).fetchall()
        affected: list[str] = []
        for run in expired:
            self.db.execute("UPDATE runs SET state='STALE' WHERE run_id=?", (run["run_id"],))
            target = "QUEUED" if run["job_id"] and self._job_is_idempotent(run["job_id"]) else "NEEDS_RECONCILIATION"
            self.db.execute("UPDATE jobs SET state=?, active_run_id=NULL WHERE job_id=? AND active_run_id=?", (target, run["job_id"], run["run_id"]))
            self._event("run", run["run_id"], "LEASE_EXPIRED", job_id=run["job_id"], disposition=target)
            affected.append(run["run_id"])
        return affected

    def ingest_result(self, envelope: dict[str, Any]) -> None:
        self._validate_envelope(envelope)
        run = self.db.execute("SELECT * FROM runs WHERE run_id=?", (envelope["run_id"],)).fetchone()
        serialized = json.dumps(envelope, sort_keys=True)
        if run is not None and run["state"] in RESULT_STATUSES and run["result_json"] == serialized:
            return
        if run is None or run["state"] != "RUNNING":
            raise StaleResultError("run is stale or unknown")
        if any(run[key] != envelope[key] for key in ("job_id", "worker_id", "lease_id", "attempt")) or run["lease_token"] != envelope["lease_token"]:
            raise StaleResultError("result does not own active lease")
        job = self.db.execute("SELECT active_run_id, task_json FROM jobs WHERE job_id=?", (run["job_id"],)).fetchone()
        if job is None or job["active_run_id"] != run["run_id"]:
            raise StaleResultError("result is not the active attempt")
        task = json.loads(job["task_json"])
        expected_engine = self._task_engine(task)
        actual_engine = envelope.get("engine")
        if actual_engine is None and "codex_result" in envelope:
            actual_engine = expected_engine
        if actual_engine != expected_engine:
            raise ControllerError("result engine does not match task")
        result = self._result_payload(envelope)
        outcome = result["status"]
        gate = result.get("gate")
        declared_gates = task.get("human_gates", [])
        if outcome == "WAIT_USER":
            if not isinstance(gate, str) or gate not in declared_gates:
                raise ControllerError("WAIT_USER result requires a declared human gate")
        elif gate is not None:
            raise ControllerError("non-WAIT_USER result cannot carry a human gate")
        self.db.execute("UPDATE runs SET state=?, result_json=? WHERE run_id=?", (outcome, serialized, run["run_id"]))
        self.db.execute("UPDATE jobs SET state=?, active_run_id=NULL WHERE job_id=?", (outcome, run["job_id"]))
        self._event("run", run["run_id"], "RESULT_INGESTED", outcome=outcome, gate=result["gate"], engine=actual_engine)

    def status(self, job_id: str) -> dict[str, Any]:
        job = self.db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if job is None:
            raise ControllerError(f"unknown job: {job_id}")
        runs = self.db.execute("SELECT run_id, attempt, worker_id, state, lease_expires_at FROM runs WHERE job_id=? ORDER BY attempt", (job_id,)).fetchall()
        task = json.loads(job["task_json"])
        return {
            "job_id": job_id,
            "project": task["project"],
            "engine": self._task_engine(task),
            "engine_selection": task.get("engine_selection"),
            "state": job["state"],
            "attempt": job["attempt"],
            "active_run_id": job["active_run_id"],
            "runs": [dict(row) for row in runs],
        }

    def events(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.db.execute("SELECT * FROM events ORDER BY event_id")]

    def _job_is_idempotent(self, job_id: str) -> bool:
        return bool(self.db.execute("SELECT idempotent FROM jobs WHERE job_id=?", (job_id,)).fetchone()["idempotent"])

    @staticmethod
    def _hash_token(token: str, encoded: str | None = None) -> str:
        if encoded:
            algorithm, rounds, salt, _digest = encoded.split("$", 3)
            if algorithm != "pbkdf2_sha256":
                raise ControllerError("unsupported worker credential")
            salt_bytes = bytes.fromhex(salt)
        else:
            rounds, salt_bytes = 310_000, secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac("sha256", token.encode("utf-8"), salt_bytes, int(rounds)).hex()
        return f"pbkdf2_sha256${rounds}${salt_bytes.hex()}${digest}"

    @staticmethod
    def _task_engine(task: dict[str, Any]) -> str:
        explicit = task.get("execution_engine")
        if explicit:
            return explicit
        return "codex" if "codex" in task.get("required_capabilities", []) else "native"

    @staticmethod
    def _worker_matches(worker: dict[str, Any], task: dict[str, Any]) -> bool:
        target_worker = task.get("worker_id")
        if target_worker is not None and worker["worker_id"] != target_worker:
            return False
        return worker["platform"] == task["platform"] and set(task["required_capabilities"]).issubset(worker["capabilities"])

    @staticmethod
    def _validate_project(manifest: dict[str, Any]) -> None:
        required = {
            "project_id", "repository", "ref", "platform", "working_directory",
            "runtime_directory", "allowed_engines", "default_engine", "capabilities",
        }
        if not isinstance(manifest, dict) or not required.issubset(manifest):
            raise ControllerError("invalid project manifest")
        project_id = manifest["project_id"]
        if not isinstance(project_id, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", project_id):
            raise ControllerError("invalid project_id")
        if manifest["platform"] not in {"linux", "windows"}:
            raise ControllerError("invalid project platform")
        for key in ("repository", "ref", "working_directory", "runtime_directory"):
            if not isinstance(manifest.get(key), str) or not manifest[key]:
                raise ControllerError(f"invalid project field: {key}")
        if manifest["platform"] == "linux":
            for key in ("working_directory", "runtime_directory"):
                if not manifest[key].startswith("/"):
                    raise ControllerError(f"{key} must be an absolute Linux path")
        engines = manifest["allowed_engines"]
        if (
            not isinstance(engines, list)
            or not engines
            or any(engine not in ENGINE_CAPABILITIES for engine in engines)
            or len(set(engines)) != len(engines)
        ):
            raise ControllerError("invalid allowed_engines")
        default_engine = manifest["default_engine"]
        if default_engine == "auto":
            if manifest.get("engine_policy", "balanced-v1") != "balanced-v1":
                raise ControllerError("unsupported engine_policy")
        elif default_engine not in engines:
            raise ControllerError("default_engine must be allowed or auto")
        if manifest.get("engine_policy") is not None and manifest["engine_policy"] != "balanced-v1":
            raise ControllerError("unsupported engine_policy")
        capabilities = manifest["capabilities"]
        if not isinstance(capabilities, list) or any(not isinstance(item, str) or not item for item in capabilities):
            raise ControllerError("invalid project capabilities")
        if manifest.get("worker_id") is not None and (
            not isinstance(manifest["worker_id"], str) or not manifest["worker_id"]
        ):
            raise ControllerError("invalid project worker_id")
        for key in ("task_type", "execution_profile"):
            if manifest.get(key) is not None and (
                not isinstance(manifest[key], str) or not manifest[key]
            ):
                raise ControllerError(f"invalid project field: {key}")
        if manifest.get("allowed_paths") is not None:
            paths = manifest["allowed_paths"]
            if not isinstance(paths, list) or any(not isinstance(item, str) or not item for item in paths):
                raise ControllerError("invalid project allowed_paths")
        if manifest.get("workspaces") is not None:
            workspaces = manifest["workspaces"]
            if not isinstance(workspaces, dict):
                raise ControllerError("invalid project workspaces")
            for name, path in workspaces.items():
                if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", name):
                    raise ControllerError("invalid project workspace name")
                if not isinstance(path, str) or not path:
                    raise ControllerError("invalid project workspace path")
                if manifest["platform"] == "linux" and not path.startswith("/"):
                    raise ControllerError("project workspace path must be absolute")
        if manifest.get("timeout_seconds") is not None and (
            not isinstance(manifest["timeout_seconds"], int) or not 1 <= manifest["timeout_seconds"] <= 7200
        ):
            raise ControllerError("invalid project timeout_seconds")
        if manifest.get("max_turns") is not None and (
            not isinstance(manifest["max_turns"], int) or not 1 <= manifest["max_turns"] <= 50
        ):
            raise ControllerError("invalid project max_turns")

    @staticmethod
    def _validate_worker(worker: dict[str, Any]) -> None:
        required = {"worker_id", "platform", "environment", "capabilities", "max_concurrent_jobs"}
        if not required.issubset(worker) or worker["platform"] not in {"linux", "windows"} or not isinstance(worker["capabilities"], list):
            raise ControllerError("invalid worker descriptor")

    @staticmethod
    def _validate_task(task: dict[str, Any]) -> None:
        required = {"brain", "project", "repository", "ref", "task_type", "platform", "required_capabilities", "execution_profile", "human_gates", "idempotency_policy"}
        if not required.issubset(task) or task["idempotency_policy"] not in {"safe_retry", "manual_reconcile"}:
            raise ControllerError("invalid task snapshot")
        brain = task["brain"]
        if not isinstance(brain, dict) or not {"repository", "ref", "commit"}.issubset(brain):
            raise ControllerError("task requires immutable brain reference")
        task_file = brain.get("task_file")
        task_text = task.get("task_text")
        if not task_file and not (isinstance(task_text, str) and task_text.strip()):
            raise ControllerError("task requires brain.task_file or inline task_text")
        if task_text is not None:
            if not isinstance(task_text, str) or not task_text.strip():
                raise ControllerError("invalid inline task_text")
            if len(task_text.encode("utf-8")) > 1_048_576:
                raise ControllerError("inline task_text exceeds 1 MiB")
            if str(brain.get("repository", "")).startswith("inline://"):
                expected = hashlib.sha256(task_text.encode("utf-8")).hexdigest()
                if brain.get("commit") != expected:
                    raise ControllerError("inline task_text hash does not match brain commit")
        capabilities = task.get("required_capabilities")
        if not isinstance(capabilities, list) or any(not isinstance(item, str) or not item for item in capabilities):
            raise ControllerError("invalid required_capabilities")
        if task.get("worker_id") is not None and (not isinstance(task["worker_id"], str) or not task["worker_id"]):
            raise ControllerError("invalid worker_id")
        engine = task.get("execution_engine")
        if engine is not None:
            if engine not in ENGINE_CAPABILITIES:
                raise ControllerError("invalid execution_engine")
            capabilities = task.get("required_capabilities")
            if not isinstance(capabilities, list) or not ENGINE_CAPABILITIES[engine].issubset(capabilities):
                raise ControllerError("execution_engine capabilities are missing")

    @staticmethod
    def _result_payload(envelope: dict[str, Any]) -> dict[str, Any]:
        if "engine_result" in envelope:
            return envelope["engine_result"]
        return envelope["codex_result"]

    @staticmethod
    def _validate_envelope(envelope: dict[str, Any]) -> None:
        required = {"job_id", "run_id", "attempt", "worker_id", "lease_id", "lease_token", "started_at", "finished_at", "artifacts", "hashes"}
        if not required.issubset(envelope):
            raise ControllerError("invalid result envelope")
        has_engine_result = "engine_result" in envelope
        has_legacy_result = "codex_result" in envelope
        if has_engine_result == has_legacy_result:
            raise ControllerError("result envelope must contain exactly one result payload")
        if has_engine_result:
            if envelope.get("engine") not in ENGINE_CAPABILITIES:
                raise ControllerError("invalid result engine")
        elif envelope.get("engine") not in {None, "codex"}:
            raise ControllerError("legacy codex_result cannot use another engine")
        result = Controller._result_payload(envelope)
        if not isinstance(result, dict) or result.get("status") not in RESULT_STATUSES:
            raise ControllerError("invalid engine result")
        result_required = {"status", "summary", "gate", "completed", "remaining", "evidence"}
        if not result_required.issubset(result):
            raise ControllerError("engine result does not match Hermes runner contract")
