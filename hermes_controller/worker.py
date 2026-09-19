"""Outbound-polling worker daemon for Hermes Phase 2A."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any
from urllib.error import URLError, HTTPError
from urllib.request import Request, urlopen

from .adapters import AdapterResult, ClaudeAgentAdapter, CodexRunAdapter, CxhRunAdapter, EngineRoutingAdapter, ExecutionAdapter, HybridAdapter, MockAdapter


class TransportError(RuntimeError): pass


class HTTPControllerClient:
    def __init__(self, url: str, worker_id: str, token: str, timeout_s: float = 5.0) -> None:
        self.url, self.worker_id, self.token, self.timeout_s = url.rstrip("/"), worker_id, token, timeout_s

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = json.dumps(payload).encode() if payload is not None else None
        req = Request(self.url + path, data=data, method=method, headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json", "X-Hermes-Worker-Id": self.worker_id})
        try:
            with urlopen(req, timeout=self.timeout_s) as response: return json.loads(response.read())
        except (HTTPError, URLError, OSError) as exc: raise TransportError(str(exc)) from exc

    def enrol(self, descriptor: dict[str, Any]) -> dict[str, Any]: return self.request("POST", "/v1/workers/enrol", descriptor)
    def heartbeat_worker(self) -> dict[str, Any]: return self.request("POST", "/v1/workers/heartbeat", {"worker_id": self.worker_id})
    def claim(self) -> dict[str, Any] | None: return self.request("POST", "/v1/jobs/claim", {"worker_id": self.worker_id})["claim"]
    def heartbeat_run(self, claim: dict[str, Any]) -> dict[str, Any]: return self.request("POST", "/v1/runs/heartbeat", {"worker_id": self.worker_id, **{key: claim[key] for key in ("run_id", "lease_id", "lease_token")}})
    def ingest(self, envelope: dict[str, Any]) -> dict[str, Any]: return self.request("POST", "/v1/runs/result", envelope)


class WorkerDaemon:
    def __init__(self, config: dict[str, Any], adapter: ExecutionAdapter | None = None) -> None:
        self.config = config
        self.worker = config["worker"]
        self.client = HTTPControllerClient(config["controller_url"], self.worker["worker_id"], config["token"])
        self.adapter = adapter or self._adapter_from_worker_config(config)
        self.heartbeat_interval_s = config.get("heartbeat_interval_ms", 30_000) / 1000
        self.backoff_s, self.max_backoff_s = config.get("retry_backoff_ms", 1_000) / 1000, config.get("max_backoff_ms", 30_000) / 1000
        self.state_path = Path(config.get("state_file", "worker-state.json"))
        roots_env = config.get("task_materialization_roots_env")
        self.task_materialization_roots = [
            Path(item).resolve()
            for item in (os.environ.get(roots_env, "").split(os.pathsep) if roots_env else [])
            if item
        ]
        saved = self._load_state()
        self.active_claim: dict[str, Any] | None = saved.get("claim") if saved else None
        self.pending_envelope: dict[str, Any] | None = saved.get("pending_envelope") if saved else None

    @classmethod
    def from_file(cls, path: str | Path, adapter: ExecutionAdapter | None = None) -> "WorkerDaemon":
        config = json.loads(Path(path).read_text(encoding="utf-8"))
        token_env = config.pop("token_env", None)
        if token_env:
            import os
            config["token"] = os.environ[token_env]
        return cls(config, adapter)

    @classmethod
    def _adapter_from_worker_config(cls, config: dict[str, Any]) -> ExecutionAdapter:
        engines = config.get("adapters")
        if engines is None:
            return cls._adapter_from_config(config.get("adapter", {"kind": "mock"}))
        if not isinstance(engines, dict) or not engines:
            raise ValueError("adapters must be a non-empty object")
        unknown = set(engines) - {"codex", "claude", "hybrid", "native"}
        if unknown:
            raise ValueError(f"unknown execution engine adapters: {sorted(unknown)}")
        default_engine = config.get("default_execution_engine", "codex")
        adapters: dict[str, ExecutionAdapter] = {}
        for name, spec in engines.items():
            if spec.get("kind") != "hybrid":
                adapters[name] = cls._adapter_from_config(spec)
        for name, spec in engines.items():
            if spec.get("kind") == "hybrid":
                primary_name = spec.get("primary_engine", "claude")
                reviewer_name = spec.get("review_engine", "codex")
                if primary_name not in adapters or reviewer_name not in adapters:
                    raise ValueError("hybrid adapter references an unavailable engine")
                adapters[name] = HybridAdapter(
                    adapters[primary_name], adapters[reviewer_name],
                    max_review_rounds=spec.get("max_review_rounds", 1),
                )
        return EngineRoutingAdapter(adapters, default_engine=default_engine)

    @staticmethod
    def _adapter_from_config(config: dict[str, Any]) -> ExecutionAdapter:
        kind = config.get("kind", "mock")
        if kind == "mock": return MockAdapter()
        import os
        if kind in {"cxh-run", "codex-run"}:
            runner = os.environ[config["runner_path_env"]]
            roots = [item for item in os.environ[config["authorized_roots_env"]].split(os.pathsep) if item]
            profiles = set(config.get("execution_profiles", ["hermes"]))
            types = set(config.get("task_types", ["development", "hermes_smoke"]))
            adapter_type = CodexRunAdapter if kind == "codex-run" else CxhRunAdapter
            return adapter_type(runner_path=runner, authorized_roots=roots, execution_profiles=profiles, task_types=types)
        if kind == "claude-agent":
            roots = [item for item in os.environ[config["authorized_roots_env"]].split(os.pathsep) if item]
            profiles = set(config.get("execution_profiles", ["claude_smoke", "hermes"]))
            types = set(config.get("task_types", ["claude_smoke", "development"]))
            cli_path_env = config.get("cli_path_env")
            cli_path = os.environ.get(cli_path_env) if cli_path_env else None
            return ClaudeAgentAdapter(
                authorized_roots=roots, execution_profiles=profiles, task_types=types,
                max_turns=config.get("max_turns", 6), subscription_only=config.get("subscription_only", True),
                cli_path=cli_path,
            )
        raise ValueError("unknown worker adapter")

    def _load_state(self) -> dict[str, Any] | None:
        return json.loads(self.state_path.read_text()) if self.state_path.exists() else None

    def _save_state(self, claim: dict[str, Any] | None, pending_envelope: dict[str, Any] | None = None) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        if claim is None:
            self.state_path.unlink(missing_ok=True)
        else:
            self.state_path.write_text(json.dumps({"claim": claim, "pending_envelope": pending_envelope}, sort_keys=True), encoding="utf-8")

    def register(self) -> None: self.client.enrol(self.worker)

    def _execution_engine_for_task(self, task: dict[str, Any]) -> str:
        explicit = task.get("execution_engine")
        if explicit in {"codex", "claude", "hybrid", "native"}:
            return explicit
        return "codex" if "codex" in task.get("required_capabilities", []) else "native"

    def _materialize_inline_task(self, task: dict[str, Any]) -> dict[str, Any]:
        task_text = task.get("task_text")
        if task_text is None:
            return task
        if not isinstance(task_text, str) or not task_text.strip():
            raise ValueError("inline task_text is invalid")
        if len(task_text.encode("utf-8")) > 1_048_576:
            raise ValueError("inline task_text exceeds 1 MiB")
        run_output = task.get("run_output_dir")
        if not isinstance(run_output, str) or not run_output:
            raise ValueError("inline tasks require run_output_dir")
        target_dir = Path(run_output).resolve()
        if not self.task_materialization_roots:
            raise ValueError("inline task materialization is not configured")
        if not any(target_dir == root or root in target_dir.parents for root in self.task_materialization_roots):
            raise ValueError("run_output_dir is outside task materialization roots")
        brain = task.get("brain")
        if not isinstance(brain, dict):
            raise ValueError("inline task requires brain metadata")
        if str(brain.get("repository", "")).startswith("inline://"):
            digest = hashlib.sha256(task_text.encode("utf-8")).hexdigest()
            if brain.get("commit") != digest:
                raise ValueError("inline task hash does not match brain commit")
        target_dir.mkdir(parents=True, exist_ok=True)
        task_file = target_dir / "hermes-task.md"
        task_file.write_text(task_text, encoding="utf-8")
        task_file.chmod(0o600)
        prepared = json.loads(json.dumps(task))
        prepared_brain = dict(prepared["brain"])
        prepared_brain["task_file"] = str(task_file)
        prepared["brain"] = prepared_brain
        return prepared

    def once(self) -> bool:
        self.client.heartbeat_worker()
        claim = self.active_claim or self.client.claim()
        if not claim: return False
        self.active_claim = claim; self._save_state(claim, self.pending_envelope)
        if self.pending_envelope:
            self.client.ingest(self.pending_envelope)
            self.pending_envelope = None; self.active_claim = None; self._save_state(None)
            return True
        result_box: list[Any] = []
        def execute_claim() -> None:
            try:
                prepared_task = self._materialize_inline_task(claim["task"])
                result_box.append(self.adapter.execute(prepared_task))
            except ValueError as exc:
                result_box.append(AdapterResult(status="BLOCKED", summary=f"worker task preparation blocked: {exc}"))
            except OSError as exc:
                result_box.append(AdapterResult(status="FAILED", summary=f"worker task preparation failed: {type(exc).__name__}"))
        thread = threading.Thread(target=execute_claim, daemon=True)
        thread.start()
        while thread.is_alive():
            thread.join(self.heartbeat_interval_s)
            if thread.is_alive():
                self.client.heartbeat_worker(); self.client.heartbeat_run(claim)
        if not result_box:
            raise RuntimeError("adapter did not return a result")
        result = result_box[0]
        now = time.time_ns() // 1_000_000
        envelope = {**{key: claim[key] for key in ("job_id", "run_id", "attempt", "lease_id", "lease_token")}, "worker_id": self.worker["worker_id"], "started_at": now, "finished_at": now, "engine": self._execution_engine_for_task(claim["task"]), "engine_result": result.result(), "artifacts": [], "hashes": {}}
        self.pending_envelope = envelope; self._save_state(claim, envelope)
        self.client.ingest(envelope)
        self.pending_envelope = None; self.active_claim = None; self._save_state(None)
        return True

    def run(self, stop: threading.Event | None = None) -> None:
        self.register()
        delay = self.backoff_s
        while stop is None or not stop.is_set():
            try:
                self.once(); delay = self.backoff_s; time.sleep(min(self.heartbeat_interval_s, 1))
            except TransportError:
                time.sleep(delay); delay = min(delay * 2, self.max_backoff_s)
