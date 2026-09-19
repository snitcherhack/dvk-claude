"""Hermes Controller and worker CLI."""

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


def _instruction(args: argparse.Namespace) -> str:
    if getattr(args, "instruction", None) is not None:
        return args.instruction
    return Path(args.instruction_file).read_text(encoding="utf-8")


def _add_instruction_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--instruction")
    group.add_argument("--instruction-file")
    parser.add_argument("--engine", choices=("codex", "claude", "hybrid", "native"))
    parser.add_argument("--idempotency-key")


def main() -> None:
    parser = argparse.ArgumentParser(prog="hermes-controller")
    parser.add_argument("--runtime-root", required=True)
    commands = parser.add_subparsers(dest="command", required=True)

    for name in ("register-worker", "enqueue", "ingest-result"):
        sub = commands.add_parser(name)
        sub.add_argument("json_file")

    commands.add_parser("claim").add_argument("worker_id")
    renew = commands.add_parser("renew-lease")
    renew.add_argument("run_id")
    renew.add_argument("lease_id")
    renew.add_argument("lease_token")
    commands.add_parser("heartbeat-worker").add_argument("worker_id")
    commands.add_parser("worker-status").add_argument("worker_id")
    commands.add_parser("reconcile-leases")
    commands.add_parser("status").add_argument("job_id")

    project = commands.add_parser("project")
    project_sub = project.add_subparsers(dest="project_command", required=True)
    project_sub.add_parser("register").add_argument("json_file")
    project_sub.add_parser("validate").add_argument("json_file")
    project_sub.add_parser("list")
    project_sub.add_parser("show").add_argument("project_id")
    project_sub.add_parser("remove").add_argument("project_id")

    task = commands.add_parser("task")
    task_sub = task.add_subparsers(dest="task_command", required=True)
    build = task_sub.add_parser("build")
    build.add_argument("project_id")
    _add_instruction_arguments(build)
    build.add_argument("--output")
    create = task_sub.add_parser("create")
    create.add_argument("project_id")
    _add_instruction_arguments(create)

    serve_parser = commands.add_parser("serve")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8787)
    serve_parser.add_argument(
        "--enrollment-token-env",
        action="append",
        default=[],
        metavar="WORKER_ID=ENV_VAR",
    )

    worker = commands.add_parser("worker")
    worker_sub = worker.add_subparsers(dest="worker_command", required=True)
    for name in ("run", "once"):
        worker_sub.add_parser(name).add_argument("config_file")

    args = parser.parse_args()

    if args.command == "worker":
        daemon = WorkerDaemon.from_file(args.config_file)
        if args.worker_command == "once":
            daemon.register()
            print(json.dumps({"worked": daemon.once()}))
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
            try:
                server.serve_forever()
            finally:
                server.server_close()
            return

        if args.command == "project":
            if args.project_command == "register":
                out = {"project_id": controller.register_project(_read(args.json_file))}
            elif args.project_command == "validate":
                manifest = _read(args.json_file)
                controller._validate_project(manifest)
                out = {"project_id": manifest["project_id"], "valid": True}
            elif args.project_command == "list":
                out = controller.list_projects()
            elif args.project_command == "show":
                out = controller.project(args.project_id)
            else:
                controller.remove_project(args.project_id)
                out = {"project_id": args.project_id, "removed": True}
            print(json.dumps(out, sort_keys=True))
            return

        if args.command == "task":
            task_spec = controller.build_project_task(
                args.project_id,
                _instruction(args),
                engine=args.engine,
                idempotency_key=args.idempotency_key,
            )
            if args.task_command == "build":
                if args.output:
                    Path(args.output).write_text(
                        json.dumps(task_spec, sort_keys=True, indent=2) + "\n",
                        encoding="utf-8",
                    )
                    out = {"job_id": task_spec["job_id"], "output": str(Path(args.output))}
                    print(json.dumps(out, sort_keys=True))
                else:
                    print(json.dumps(task_spec, sort_keys=True, indent=2))
            else:
                job_id = controller.enqueue(task_spec)
                print(json.dumps({
                    "job_id": job_id,
                    "project": args.project_id,
                    "engine": task_spec["execution_engine"],
                }, sort_keys=True))
            return

        if args.command == "register-worker":
            out = controller.register_worker(_read(args.json_file))
        elif args.command == "enqueue":
            out = controller.enqueue(_read(args.json_file))
        elif args.command == "claim":
            out = controller.claim(args.worker_id)
        elif args.command == "renew-lease":
            out = controller.heartbeat_run(args.run_id, args.lease_id, args.lease_token) or {"renewed": True}
        elif args.command == "heartbeat-worker":
            out = controller.heartbeat_worker(args.worker_id) or {"received": True}
        elif args.command == "worker-status":
            out = controller.worker_status(args.worker_id)
        elif args.command == "ingest-result":
            out = controller.ingest_result(_read(args.json_file)) or {"ingested": True}
        elif args.command == "reconcile-leases":
            out = controller.reconcile_expired_leases()
        else:
            out = controller.status(args.job_id)
        print(json.dumps(out, sort_keys=True))
    finally:
        controller.close()


if __name__ == "__main__":
    main()
