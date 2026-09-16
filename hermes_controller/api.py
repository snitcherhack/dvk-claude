"""Small authenticated HTTP transport for the local Hermes controller."""

from __future__ import annotations

import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .controller import Controller, ControllerError, StaleResultError


class ControllerHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], controller: Controller, enrollment_tokens: dict[str, str] | None = None) -> None:
        self.controller = controller
        # Bootstrap credentials remain in process environment/configuration only.
        self.enrollment_tokens = enrollment_tokens or {}
        self.request_lock = threading.RLock()
        super().__init__(address, ControllerRequestHandler)


class ControllerRequestHandler(BaseHTTPRequestHandler):
    server: ControllerHTTPServer

    def log_message(self, _format: str, *_args: object) -> None:
        return  # callers choose their own service logging in a later phase

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        data = json.loads(self.rfile.read(length) or b"{}")
        if not isinstance(data, dict):
            raise ControllerError("JSON body must be an object")
        return data

    def _token(self) -> str:
        value = self.headers.get("Authorization", "")
        return value[7:] if value.startswith("Bearer ") else ""

    def _worker_auth(self, worker_id: str) -> bool:
        return self.server.controller.authenticate_worker(worker_id, self._token())

    def _reply(self, status: int, payload: Any) -> None:
        data = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        with self.server.request_lock:
            self._do_GET()

    def _do_GET(self) -> None:
        try:
            if self.path == "/health":
                self._reply(200, {"status": "ok"})
                return
            prefix = "/v1/jobs/"
            if self.path.startswith(prefix):
                worker_id = self.headers.get("X-Hermes-Worker-Id", "")
                if not self._worker_auth(worker_id): self._reply(401, {"error": "unauthorized"}); return
                self._reply(200, self.server.controller.status(self.path[len(prefix):]))
                return
            prefix = "/v1/workers/"
            if self.path.startswith(prefix):
                worker_id = self.path[len(prefix):]
                if not self._worker_auth(worker_id): self._reply(401, {"error": "unauthorized"}); return
                self._reply(200, self.server.controller.worker_status(worker_id))
                return
            self._reply(404, {"error": "not found"})
        except ControllerError as exc:
            self._reply(404, {"error": str(exc)})

    def do_POST(self) -> None:
        with self.server.request_lock:
            self._do_POST()

    def _do_POST(self) -> None:
        try:
            body = self._body()
            if self.path == "/v1/workers/enrol":
                worker_id = body.get("worker_id", "")
                expected = self.server.enrollment_tokens.get(worker_id, "")
                enrolled = self.server.controller.authenticate_worker(worker_id, self._token())
                bootstrap = bool(expected and hmac.compare_digest(expected, self._token()))
                if not enrolled and not bootstrap:
                    self._reply(401, {"error": "unauthorized"}); return
                if bootstrap and not enrolled:
                    self.server.controller.provision_worker_token(worker_id, self._token())
                self._reply(200, {"worker_id": self.server.controller.register_worker(body), "enrolled": True})
                return
            worker_id = body.get("worker_id", "")
            if not self._worker_auth(worker_id): self._reply(401, {"error": "unauthorized"}); return
            if self.path == "/v1/workers/heartbeat":
                self.server.controller.heartbeat_worker(worker_id); self._reply(200, {"received": True}); return
            if self.path == "/v1/jobs/claim":
                self._reply(200, {"claim": self.server.controller.claim(worker_id)}); return
            if self.path == "/v1/runs/heartbeat":
                self.server.controller.heartbeat_run(body["run_id"], body["lease_id"], body["lease_token"]); self._reply(200, {"renewed": True}); return
            if self.path == "/v1/runs/result":
                self.server.controller.ingest_result(body); self._reply(200, {"ingested": True}); return
            self._reply(404, {"error": "not found"})
        except StaleResultError as exc:
            self._reply(409, {"error": str(exc)})
        except (ControllerError, KeyError, ValueError, json.JSONDecodeError) as exc:
            self._reply(400, {"error": str(exc)})


def serve(controller: Controller, host: str = "127.0.0.1", port: int = 8787, enrollment_tokens: dict[str, str] | None = None) -> ControllerHTTPServer:
    return ControllerHTTPServer((host, port), controller, enrollment_tokens)
