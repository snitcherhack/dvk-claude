"""Outbound-polling worker daemon for Hermes Phase 2A."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any
from urllib.error import URLError, HTTPError
from urllib.request import Request, urlopen

from .adapters import ExecutionAdapter, MockAdapter


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
        self.adapter = adapter or MockAdapter()
        self.heartbeat_interval_s = config.get("heartbeat_interval_ms", 30_000) / 1000
        self.backoff_s, self.max_backoff_s = config.get("retry_backoff_ms", 1_000) / 1000, config.get("max_backoff_ms", 30_000) / 1000
        self.state_path = Path(config.get("state_file", "worker-state.json"))
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

    def _load_state(self) -> dict[str, Any] | None:
        return json.loads(self.state_path.read_text()) if self.state_path.exists() else None

    def _save_state(self, claim: dict[str, Any] | None, pending_envelope: dict[str, Any] | None = None) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        if claim is None:
            self.state_path.unlink(missing_ok=True)
        else:
            self.state_path.write_text(json.dumps({"claim": claim, "pending_envelope": pending_envelope}, sort_keys=True), encoding="utf-8")

    def register(self) -> None: self.client.enrol(self.worker)

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
        thread = threading.Thread(target=lambda: result_box.append(self.adapter.execute(claim["task"])), daemon=True)
        thread.start()
        while thread.is_alive():
            thread.join(self.heartbeat_interval_s)
            if thread.is_alive():
                self.client.heartbeat_worker(); self.client.heartbeat_run(claim)
        if not result_box:
            raise RuntimeError("adapter did not return a result")
        result = result_box[0]
        now = time.time_ns() // 1_000_000
        envelope = {**{key: claim[key] for key in ("job_id", "run_id", "attempt", "lease_id", "lease_token")}, "worker_id": self.worker["worker_id"], "started_at": now, "finished_at": now, "codex_result": result.result(), "artifacts": [], "hashes": {}}
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
