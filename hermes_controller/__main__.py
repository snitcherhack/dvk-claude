"""Minimal local-only Controller CLI."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .controller import Controller


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
    args = parser.parse_args()
    controller = Controller(args.runtime_root)
    try:
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
