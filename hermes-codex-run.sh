#!/usr/bin/env bash

# Project-agnostic Hermes Codex runner.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
HOME_DIR="${HOME:?HOME no está definido}"
STATE_DIR="${XDG_STATE_HOME:-$HOME_DIR/.local/state}/hermes"
SCHEMA_FILE="$SCRIPT_DIR/hermes-run-result.schema.json"
CODEX_BIN="${HERMES_CODEX_CLI:-}"
if [[ -z "$CODEX_BIN" && -x "$HOME_DIR/.local/bin/codex" ]]; then
    CODEX_BIN="$HOME_DIR/.local/bin/codex"
fi
if [[ -z "$CODEX_BIN" ]]; then
    CODEX_BIN="$(command -v codex || true)"
fi
SMOKE_TEST=0
WORKING_DIRECTORY="$(pwd)"
TASK_FILE=""
RUN_OUTPUT_DIR=""
EXECUTION_PROFILE="hermes"
TIMEOUT_SECONDS=""
DRY_RUN=0
ALLOWED_PATHS=()
PASSTHROUGH_ARGS=()
STAGE_SCHEMA=""
PROBE_HIDDEN=()

usage() {
    cat <<'EOF'
Uso: hermes-codex-run.sh [opciones]

Opciones:
  --smoke-test
  --working-directory RUTA
  --task-file RUTA
  --run-output-dir RUTA
  --allowed-path RUTA        Puede repetirse
  --execution-profile PERFIL
  --timeout-seconds SEGUNDOS
  --stage-schema NOMBRE      Solo con --execution-profile brainstorm:
                             proposals|evaluation|refinement|validation
  --probe-hidden RUTA        Solo brainstorm; ruta que la sonda exige invisible
  --dry-run
  -h, --help
EOF
}

while (( $# )); do
    case "$1" in
        -h|--help) usage; exit 0 ;;
        --smoke-test) SMOKE_TEST=1 ;;
        --working-directory) WORKING_DIRECTORY="${2:?--working-directory requiere valor}"; shift ;;
        --task-file) TASK_FILE="${2:?--task-file requiere valor}"; shift ;;
        --run-output-dir) RUN_OUTPUT_DIR="${2:?--run-output-dir requiere valor}"; shift ;;
        --allowed-path) ALLOWED_PATHS+=("${2:?--allowed-path requiere valor}"); shift ;;
        --execution-profile) EXECUTION_PROFILE="${2:?--execution-profile requiere valor}"; shift ;;
        --timeout-seconds) TIMEOUT_SECONDS="${2:?--timeout-seconds requiere valor}"; shift ;;
        --stage-schema) STAGE_SCHEMA="${2:?--stage-schema requiere valor}"; shift ;;
        --probe-hidden) PROBE_HIDDEN+=("${2:?--probe-hidden requiere valor}"); shift ;;
        --dry-run) DRY_RUN=1 ;;
        *) PASSTHROUGH_ARGS+=("$1") ;;
    esac
    shift
done

realpath_within() {
    local candidate root
    candidate="$(realpath -m "$1")"
    root="$(realpath -m "$2")"
    [[ "$candidate" == "$root" || "$candidate" == "$root"/* ]]
}

LOCK_DIR="$STATE_DIR/hermes-codex-run.lock"

acquire_lock() {
    if mkdir "$LOCK_DIR" 2>/dev/null; then
        printf '%s\n' "$$" > "$LOCK_DIR/pid"
        return 0
    fi
    local owner_pid=""
    if [[ -f "$LOCK_DIR/pid" ]]; then
        owner_pid="$(<"$LOCK_DIR/pid")"
    fi
    if [[ "$owner_pid" =~ ^[0-9]+$ ]] && kill -0 "$owner_pid" 2>/dev/null; then
        echo "ERROR: Hermes Codex ya tiene una ejecución activa" >&2
        exit 1
    fi
    rm -f "$LOCK_DIR/pid"
    rmdir "$LOCK_DIR" 2>/dev/null || {
        echo "ERROR: no se pudo recuperar el lock de Hermes Codex" >&2
        exit 1
    }
    mkdir "$LOCK_DIR"
    printf '%s\n' "$$" > "$LOCK_DIR/pid"
}

release_lock() {
    if [[ -d "$LOCK_DIR" && -f "$LOCK_DIR/pid" ]] && [[ "$(<"$LOCK_DIR/pid")" == "$$" ]]; then
        rm -f "$LOCK_DIR/pid"
        rmdir "$LOCK_DIR" 2>/dev/null || true
    fi
}

if [[ "$EXECUTION_PROFILE" == "brainstorm" ]]; then
    # shellcheck source=hermes-codex-brainstorm.sh
    source "$SCRIPT_DIR/hermes-codex-brainstorm.sh"
    run_brainstorm_stage
    exit $?
fi
if [[ -n "$STAGE_SCHEMA" || ${#PROBE_HIDDEN[@]} -gt 0 ]]; then
    echo "ERROR: --stage-schema y --probe-hidden solo se aceptan con --execution-profile brainstorm" >&2
    exit 2
fi

if [[ "$EXECUTION_PROFILE" != "hermes" && "$EXECUTION_PROFILE" != "review" ]]; then
    echo "ERROR: execution profile no permitido: $EXECUTION_PROFILE" >&2
    exit 2
fi
if [[ -z "$TASK_FILE" ]]; then
    echo "ERROR: --task-file es obligatorio" >&2
    exit 2
fi
if [[ -z "$RUN_OUTPUT_DIR" ]]; then
    echo "ERROR: --run-output-dir es obligatorio" >&2
    exit 2
fi
if [[ ! -d "$WORKING_DIRECTORY" || ! -d "$WORKING_DIRECTORY/.git" ]]; then
    echo "ERROR: working directory no es un repositorio Git: $WORKING_DIRECTORY" >&2
    exit 2
fi
if [[ ! -f "$TASK_FILE" ]]; then
    echo "ERROR: task file inexistente: $TASK_FILE" >&2
    exit 2
fi
if [[ ! -f "$SCHEMA_FILE" ]]; then
    echo "ERROR: schema inexistente: $SCHEMA_FILE" >&2
    exit 1
fi
if [[ -z "$CODEX_BIN" || ! -x "$CODEX_BIN" ]]; then
    echo "ERROR: Codex CLI no disponible" >&2
    exit 2
fi
if [[ -n "$TIMEOUT_SECONDS" ]] && ! [[ "$TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]]; then
    echo "ERROR: timeout inválido" >&2
    exit 2
fi
if (( SMOKE_TEST )) && (( ${#PASSTHROUGH_ARGS[@]} > 0 )); then
    echo "ERROR: --smoke-test no acepta argumentos adicionales." >&2
    exit 2
fi

if (( ${#ALLOWED_PATHS[@]} == 0 )); then
    echo "ERROR: se requiere al menos un --allowed-path" >&2
    exit 2
fi

for required_path in "$WORKING_DIRECTORY" "$TASK_FILE" "$RUN_OUTPUT_DIR"; do
    authorized=0
    for root in "${ALLOWED_PATHS[@]}"; do
        if realpath_within "$required_path" "$root"; then
            authorized=1
            break
        fi
    done
    if (( ! authorized )); then
        echo "ERROR: ruta fuera de allowed paths: $required_path" >&2
        exit 2
    fi
done

if (( DRY_RUN )); then
    echo "cwd: $WORKING_DIRECTORY"
    echo "task: $TASK_FILE"
    echo "run_output: $RUN_OUTPUT_DIR"
    printf 'allowed_path: %s\n' "${ALLOWED_PATHS[@]}"
    echo "schema: $SCHEMA_FILE"
    echo "smoke_test: $SMOKE_TEST"
    echo "codex: $CODEX_BIN"
    echo "command: $CODEX_BIN exec --profile hermes --sandbox workspace-write --output-schema <schema> --output-last-message <result> [--add-dir ...] <bootstrap>"
    exit 0
fi

mkdir -p "$STATE_DIR" "$RUN_OUTPUT_DIR"
acquire_lock
trap release_lock EXIT HUP INT TERM

LOG_FILE="$RUN_OUTPUT_DIR/codex-exec.log"
RESULT_FILE="$RUN_OUTPUT_DIR/result.json"
SMOKE_WORKSPACE="$RUN_OUTPUT_DIR/smoke-workspace"

SANDBOX_MODE="workspace-write"
if (( SMOKE_TEST )); then
    mkdir -p "$SMOKE_WORKSPACE"
    BOOTSTRAP="Read the Hermes task snapshot at $TASK_FILE, but do not execute the requested project work. Perform only a safe runner smoke test: run git status --short --branch in the working directory, create a new disposable Git repository under $SMOKE_WORKSPACE, create a marker file there, run git add on that marker to verify .git/index writes, then delete only that disposable repository. Do not use network, do not modify the real repository, do not commit or push. Return status DONE only after all checks complete; gate null; remaining empty; evidence with verifiable paths/results."
elif [[ "$EXECUTION_PROFILE" == "review" ]]; then
    SANDBOX_MODE="read-only"
    BOOTSTRAP="Read the Hermes review task at $TASK_FILE. Review the current working tree only; do not modify files, do not use network, do not commit or push. Return DONE when no material correctness, security, regression, or task-compliance issue remains. Return BLOCKED when you find one or more material issues that require another implementation pass, and describe each issue precisely in summary/evidence. Return FAILED only for an unrecoverable review failure. Return only the structured Hermes result."
else
    BOOTSTRAP="Read the Hermes task snapshot at $TASK_FILE and execute only that task inside the declared allowed paths. Continue until exactly one terminal state applies: DONE when all task success conditions are complete; WAIT_USER at a declared human gate; BLOCKED for a real technical or permission blocker; FAILED for an unrecoverable failure. Do not invent gates. Do not commit, push, publish, or access paths outside the declared scope unless the task explicitly authorizes it. Return only the structured Hermes result."
fi

ARGS=(
    exec
    --profile hermes
    --sandbox "$SANDBOX_MODE"
    --output-schema "$SCHEMA_FILE"
    --output-last-message "$RESULT_FILE"
)

declare -A ADD_DIR_SEEN=()
for root in "${ALLOWED_PATHS[@]}"; do
    candidate="$(realpath -m "$root")"
    if [[ -f "$candidate" ]]; then
        candidate="$(dirname "$candidate")"
    fi
    if [[ -d "$candidate" && -z "${ADD_DIR_SEEN[$candidate]:-}" ]]; then
        ARGS+=(--add-dir "$candidate")
        ADD_DIR_SEEN["$candidate"]=1
    fi
done

if (( SMOKE_TEST )) || [[ "$EXECUTION_PROFILE" == "review" ]]; then
    ARGS+=(--config sandbox_workspace_write.network_access=false)
fi

set +e
cd "$WORKING_DIRECTORY"
if [[ -n "$TIMEOUT_SECONDS" ]]; then
    timeout --foreground "$TIMEOUT_SECONDS" "$CODEX_BIN" "${ARGS[@]}" "$BOOTSTRAP" "${PASSTHROUGH_ARGS[@]}" < /dev/null 2>&1 | tee "$LOG_FILE"
else
    "$CODEX_BIN" "${ARGS[@]}" "$BOOTSTRAP" "${PASSTHROUGH_ARGS[@]}" < /dev/null 2>&1 | tee "$LOG_FILE"
fi
CODEX_STATUS="${PIPESTATUS[0]}"
set -e

VALIDATION_STATUS=0
if ! python3 - "$RESULT_FILE" "$SMOKE_TEST" <<'PY'
import json
import sys
from pathlib import Path

result_path = Path(sys.argv[1])
smoke_test = sys.argv[2] == "1"
required = {"status", "summary", "gate", "completed", "remaining", "evidence"}
allowed_statuses = {"DONE", "WAIT_USER", "BLOCKED", "FAILED"}

try:
    result = json.loads(result_path.read_text(encoding="utf-8"))
except Exception as exc:
    print(f"ERROR: result.json no es JSON válido: {exc}", file=sys.stderr)
    raise SystemExit(1)

if not isinstance(result, dict) or set(result) != required:
    print("ERROR: result.json no cumple el contrato exacto de Hermes", file=sys.stderr)
    raise SystemExit(1)
if result["status"] not in allowed_statuses:
    print("ERROR: status inválido en result.json", file=sys.stderr)
    raise SystemExit(1)
if not isinstance(result["summary"], str):
    print("ERROR: summary inválido", file=sys.stderr)
    raise SystemExit(1)
if result["gate"] is not None and not isinstance(result["gate"], str):
    print("ERROR: gate inválido", file=sys.stderr)
    raise SystemExit(1)
for key in ("completed", "remaining", "evidence"):
    if not isinstance(result[key], list) or not all(isinstance(item, str) for item in result[key]):
        print(f"ERROR: {key} inválido", file=sys.stderr)
        raise SystemExit(1)
if smoke_test and result["status"] != "DONE":
    print("ERROR: el smoke test no ha devuelto DONE", file=sys.stderr)
    raise SystemExit(1)
PY
then
    VALIDATION_STATUS=1
fi

if (( CODEX_STATUS != 0 )); then
    exit "$CODEX_STATUS"
fi
exit "$VALIDATION_STATUS"
