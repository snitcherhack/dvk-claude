# Hermes project registry and task builder

Hermes projects are controller-owned manifests that describe how a repository may
be executed without hard-coding project-specific paths in the Controller.

## Project manifest

Example for a Linux project on `main-linux`:

```json
{
  "project_id": "example-api",
  "repository": "git@github.com:owner/example-api.git",
  "ref": "main",
  "platform": "linux",
  "worker_id": "main-linux",
  "working_directory": "/home/deiv/Proyectos/example-api",
  "runtime_directory": "/home/deiv/.local/state/dvk-hermes-projects/example-api",
  "allowed_engines": ["codex", "claude", "hybrid"],
  "default_engine": "auto",
  "engine_policy": "balanced-v1",
  "capabilities": ["git", "python", "tests"],
  "task_type": "development",
  "execution_profile": "hermes",
  "human_gates": [],
  "idempotency_policy": "safe_retry",
  "timeout_seconds": 900,
  "max_turns": 12
}
```

`working_directory` is the repository checkout on the target worker.
`runtime_directory` is scratch/state space for immutable task snapshots and
run outputs. A project may optionally add more task-scoped `allowed_paths`, but
the worker still applies its own maximum authorized roots.

`worker_id` is optional. When present, only that worker may claim the task even
if another worker advertises the same platform and capabilities.

## Balanced automatic engine policy

When `default_engine` is `auto`, Hermes resolves it to a concrete engine before
the job is queued. The current deterministic policy is `balanced-v1`:

```text
implementation / bug fixes / tests   -> Codex
architecture / analysis / planning   -> Claude
review / security / production /
critical changes / migrations        -> Hybrid
neutral or unmatched task            -> Codex
```

The policy never stores `auto` as the execution engine. The queued task contains
the selected concrete engine plus an auditable `engine_selection` object, for
example:

```json
{
  "execution_engine": "hybrid",
  "engine_selection": {
    "mode": "auto",
    "policy": "balanced-v1",
    "rule": "hybrid_review_or_high_impact",
    "selected_engine": "hybrid"
  }
}
```

An explicit `--engine codex|claude|hybrid|native` always overrides the automatic
policy, provided that engine is allowed by the project. `--engine auto` forces
the project policy for a single task even if the project has a concrete default.

If the preferred engine for a rule is not allowed by the project, the policy
uses a deterministic fallback and records that fallback rule in the task.

## Registry CLI

All commands use the Controller runtime root:

```bash
python3 -m hermes_controller --runtime-root /path/to/controller project validate project.json
python3 -m hermes_controller --runtime-root /path/to/controller project register project.json
python3 -m hermes_controller --runtime-root /path/to/controller project list
python3 -m hermes_controller --runtime-root /path/to/controller project show example-api
python3 -m hermes_controller --runtime-root /path/to/controller project remove example-api
```

Register is an upsert: re-registering the same `project_id` updates the
manifest while preserving the original registration timestamp.

## Building and creating tasks

Hermes can build a task without a pre-existing task file:

```bash
python3 -m hermes_controller --runtime-root /path/to/controller \
  task build example-api \
  --engine hybrid \
  --instruction-file /tmp/task.md \
  --output /tmp/hermes-task.json
```

To enqueue directly:

```bash
python3 -m hermes_controller --runtime-root /path/to/controller \
  task create example-api \
  --engine codex \
  --instruction-file /tmp/task.md
```

For short instructions `--instruction "text"` is also supported. For
automation and multiline tasks, `--instruction-file` avoids shell quoting
differences between Windows, WSL and Linux.

The task builder:

- selects only an engine allowed by the project;
- adds the engine's required capabilities automatically;
- pins an optional `worker_id`;
- generates a unique run directory;
- embeds the instruction as `task_text`;
- hashes that instruction into the immutable inline brain reference;
- limits task paths to the project workspace/runtime plus explicitly declared
  extra paths.

## Inline task materialization

Inline tasks do not require the Controller to write into a worker filesystem.

The Controller stores the instruction inside the queued task. After the worker
claims it, the worker verifies the SHA-256 task hash, checks the destination
against its configured materialization roots, and writes:

```text
<runtime_directory>/<job_id>/hermes-task.md
```

with mode `0600`. The existing Codex, Claude and Hybrid adapters then consume
that local snapshot exactly like a traditional `brain.task_file`.

This keeps the Controller independent of worker filesystems while preserving an
immutable task snapshot and the existing path-security model.

## Worker authorization model

For a general-purpose Linux worker, use broad maximum roots such as:

```text
/home/deiv/Proyectos
/home/deiv/.local/state/dvk-hermes-projects
```

These are worker-level ceilings, not per-job permissions. Every task still
carries its narrower `allowed_paths`, which Codex and Claude enforce before
accessing or changing files.

The project registry therefore does not grant arbitrary filesystem access: a
project path must be inside both the worker maximum roots and the task's
allowed-path scope.

## Job status result contract

`Controller.status(job_id)` preserves the job and run summary fields and adds a
stable result surface for Director and gateway integrations:

```json
{
  "result": {
    "status": "DONE",
    "summary": "Task completed",
    "gate": null,
    "completed": [],
    "remaining": [],
    "evidence": []
  },
  "artifacts": [],
  "hashes": {}
}
```

`result` is only the normalized Hermes payload shown above, whether the worker
submitted a modern `engine_result` envelope or the legacy `codex_result`
envelope. The raw envelope, lease identifiers/tokens, and worker credentials
are never included in serialized status output. `artifacts` and `hashes` are
exposed as separate top-level fields.

Before any attempt reaches a terminal state, these fields are `result: null`,
`artifacts: []`, and `hashes: {}`. If a job has multiple attempts, status uses
the highest-numbered terminal attempt. Data attached to a stale or still
running attempt is never promoted to `result`.

If the latest terminal attempt contains malformed historical result data,
status preserves the job/run summary but returns the empty result surface
instead of failing the request or promoting an older terminal attempt.

## Human-gate contract

`WAIT_USER` is fail-closed. A worker may return it only with a gate explicitly
declared in the task/project `human_gates` list. An undeclared or missing
WAIT_USER gate is converted to `BLOCKED` by the worker and is also rejected by
the Controller if a nonconforming worker bypasses that normalization.

For `DONE`, `BLOCKED` and `FAILED`, the result gate must be null. The worker
clears accidental model-generated gates from non-WAIT_USER results before
ingestion; the Controller independently enforces the same invariant.

## Optional project workspaces

A project may declare named external workspaces that are not Git repositories.
They are not automatically granted to every job. A task must opt in explicitly
with one or more `--workspace` flags.

Example manifest fragment:

```json
{
  "workspaces": {
    "reels-staging": "/mnt/a/PROYECTOS/reels-staging"
  }
}
```

Then a task can opt into that workspace:

```bash
python3 -m hermes_controller --runtime-root /path/to/controller \
  task create reels-automation \
  --workspace reels-staging \
  --instruction-file /tmp/task.md
```

The selected workspace is copied into `selected_workspaces` and appended to the
task-scoped `allowed_paths`. Unselected workspaces remain inaccessible to the
job even though the project manifest knows about them. The worker-level
authorized roots remain the outer security boundary.
