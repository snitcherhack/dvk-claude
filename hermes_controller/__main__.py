"""Minimal local-only Controller CLI."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .api import serve
from .controller import Controller
from .worker import WorkerDaemon


def _read(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser(prog="hermes-controller")
    parser.add_argument("--runtime-root", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("register-worker", "enqueue", "ingest-result"):
        sub = commands.add_parser(name)
        sub.add_argument("json_file")
    commands.add_parser("claim").add_argument("worker_id")
    renew = commands.add_parser("renew-lease")
    renew.add_argument("run_id"); renew.add_argument("lease_id"); renew.add_argument("lease_token")
    commands.add_parser("heartbeat-worker").add_argument("worker_id")
    commands.add_parser("worker-status").add_argument("worker_id")
    commands.add_parser("reconcile-leases")
    commands.add_parser("status").add_argument("job_id")
    serve_parser = commands.add_parser("serve")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8787)
    serve_parser.add_argument("--enrollment-token-env", action="append", default=[], metavar="WORKER_ID=ENV_VAR")
    worker = commands.add_parser("worker")
    worker_sub = worker.add_subparsers(dest="worker_command", required=True)
    for name in ("run", "once"):
        worker_sub.add_parser(name).add_argument("config_file")
    args = parser.parse_args()
    if args.command == "worker":
        daemon = WorkerDaemon.from_file(args.config_file)
        if args.worker_command == "once":
            daemon.register(); print(json.dumps({"worked": daemon.once()}))
        else:
            daemon.run()
        return
    controller = Controller(args.runtime_root)
    try:
        if args.command == "serve":
            tokens = {}
            for item in args.enrollment_token_env:
                worker_id, env_name = item.split("=", 1)
                tokens[worker_id] = os.environ[env_name]
            server = serve(controller, args.host, args.port, tokens)
            try: server.serve_forever()
            finally: server.server_close()
            return
        if args.command == "register-worker": out = controller.register_worker(_read(args.json_file))
        elif args.command == "enqueue": out = controller.enqueue(_read(args.json_file))
        elif args.command == "claim": out = controller.claim(args.worker_id)
        elif args.command == "renew-lease": out = controller.heartbeat_run(args.run_id, args.lease_id, args.lease_token) or {"renewed": True}
        elif args.command == "heartbeat-worker": out = controller.heartbeat_worker(args.worker_id) or {"received": True}
        elif args.command == "worker-status": out = controller.worker_status(args.worker_id)
        elif args.command == "ingest-result": out = controller.ingest_result(_read(args.json_file)) or {"ingested": True}
        elif args.command == "reconcile-leases": out = controller.reconcile_expired_leases()
        else: out = controller.status(args.job_id)
        print(json.dumps(out, sort_keys=True))
    finally:
        controller.close()


if __name__ == "__main__":
    main()
