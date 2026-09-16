#!/usr/bin/env bash

# Hermes autónomo de una sola sesión mediante codex exec.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
HOME_DIR="${HOME:?HOME no está definido}"
QA_DIR="${WINNER_TIMELINE_QA_DIR:-$HOME_DIR/winner-timeline-portrait-qa}"
STATE_DIR="${XDG_STATE_HOME:-$HOME_DIR/.local/state}/hermes"
SCHEMA_FILE="$SCRIPT_DIR/hermes-run-result.schema.json"
SMOKE_TEST=0
PASSTHROUGH_ARGS=()
WORKING_DIRECTORY="$(pwd)"
REQUESTED_TASK_FILE=""
REQUESTED_RUN_OUTPUT_DIR=""
EXECUTION_PROFILE="hermes"
TIMEOUT_SECONDS=""
DRY_RUN=0

usage() {
    cat <<'EOF'
Uso: cxh-run.sh [opciones]

Opciones:
  --smoke-test
  --working-directory RUTA
  --task-file RUTA
  --run-output-dir RUTA
  --execution-profile PERFIL
  --timeout-seconds SEGUNDOS
  --dry-run
  -h, --help
EOF
}

while (( $# )); do
    case "$1" in
        -h|--help) usage; exit 0 ;;
        --smoke-test) SMOKE_TEST=1 ;;
        --working-directory) WORKING_DIRECTORY="${2:?--working-directory requiere valor}"; shift ;;
        --task-file) REQUESTED_TASK_FILE="${2:?--task-file requiere valor}"; shift ;;
        --run-output-dir) REQUESTED_RUN_OUTPUT_DIR="${2:?--run-output-dir requiere valor}"; shift ;;
        --execution-profile) EXECUTION_PROFILE="${2:?--execution-profile requiere valor}"; shift ;;
        --timeout-seconds) TIMEOUT_SECONDS="${2:?--timeout-seconds requiere valor}"; shift ;;
        --dry-run) DRY_RUN=1 ;;
        *) PASSTHROUGH_ARGS+=("$1") ;;
    esac
    shift
done

resolve_brain() {
    local candidate
    for candidate in \
        "${HERMES_BRAIN_DIR:-}" \
        "$(dirname "$SCRIPT_DIR")/brain" \
        "$HOME_DIR/Proyectos/brain"; do
        if [[ -n "$candidate" && -d "$candidate" ]]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}

BRAIN_DIR="$(resolve_brain || true)"
if [[ -z "$BRAIN_DIR" ]]; then
    echo "ERROR: no se ha podido localizar el brain." >&2
    exit 1
fi

TASK_FILE="$BRAIN_DIR/proyectos/youtube/TAREA_ACTIVA.md"
if [[ -n "$REQUESTED_TASK_FILE" ]]; then
    TASK_FILE="$REQUESTED_TASK_FILE"
fi
if [[ "$EXECUTION_PROFILE" != "hermes" ]]; then
    echo "ERROR: execution profile no permitido: $EXECUTION_PROFILE" >&2
    exit 2
fi
if [[ ! -d "$WORKING_DIRECTORY" ]]; then
    echo "ERROR: cwd inexistente: $WORKING_DIRECTORY" >&2
    exit 2
fi
if [[ "$(realpath -m "$TASK_FILE")" != "$(realpath -m "$BRAIN_DIR")"/* ]]; then
    echo "ERROR: task file fuera del brain autorizado" >&2
    exit 2
fi
if [[ ! -f "$TASK_FILE" ]]; then
    echo "ERROR: no existe la TAREA_ACTIVA: $TASK_FILE" >&2
    exit 1
fi
if [[ ! -f "$SCHEMA_FILE" ]]; then
    echo "ERROR: no existe el schema: $SCHEMA_FILE" >&2
    exit 1
fi

if (( DRY_RUN )); then
        echo "cwd: $WORKING_DIRECTORY"
        echo "brain: $BRAIN_DIR"
        echo "task: $TASK_FILE"
        echo "qa: $QA_DIR"
        echo "state/log: $STATE_DIR"
        echo "schema: $SCHEMA_FILE"
        echo "command: codex exec --profile hermes --sandbox workspace-write --output-schema $SCHEMA_FILE --output-last-message <result> <bootstrap>"
        exit 0
fi

if (( SMOKE_TEST )) && (( ${#PASSTHROUGH_ARGS[@]} > 0 )); then
    echo "ERROR: --smoke-test no acepta argumentos adicionales." >&2
    exit 2
fi

mkdir -p "$STATE_DIR"
LOCK_DIR="$STATE_DIR/cxh-run.lock"

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
        echo "ERROR: Hermes ya tiene una ejecución activa" >&2
        exit 1
    fi

    # Lock huérfano de una ejecución ya terminada.
    rm -f "$LOCK_DIR/pid"
    rmdir "$LOCK_DIR" 2>/dev/null || {
        echo "ERROR: Hermes ya tiene una ejecución activa" >&2
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

acquire_lock
trap release_lock EXIT HUP INT TERM

RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"
RUN_DIR="${REQUESTED_RUN_OUTPUT_DIR:-$STATE_DIR/$RUN_ID}"
mkdir -p "$RUN_DIR"
LOG_FILE="$RUN_DIR/codex-exec.log"
RESULT_FILE="$RUN_DIR/result.json"
if (( SMOKE_TEST )); then
    BOOTSTRAP="Lee TAREA_ACTIVA.md en $TASK_FILE, pero no ejecutes Winner Timeline ni modifiques ninguno de los repositorios reales. Ejecuta únicamente git status --short --branch del cwd. Después crea un directorio temporal nuevo dentro de $QA_DIR, inicializa allí un repositorio Git desechable, crea un fichero marcador, ejecuta git add sobre ese fichero para comprobar la escritura de .git/index y elimina completamente solo ese repositorio temporal que tú has creado. No uses red, no ejecutes Git sobre repositorios reales salvo el git status indicado, no hagas commits ni push. Devuelve status DONE, gate null, completed con las comprobaciones realizadas, remaining vacío y evidence con las rutas o resultados verificables. No finalices antes de completar todos estos pasos."
else
    BOOTSTRAP='Lee las instrucciones del repositorio y la TAREA_ACTIVA.md de Hermes. NO finalices simplemente porque terminaste un subpaso. Continúa ejecutando la TAREA_ACTIVA hasta que ocurra exactamente uno: DONE cuando todas las success_conditions se hayan alcanzado; WAIT_USER cuando se alcance un human_gate; BLOCKED cuando exista un bloqueo técnico real que no puedas resolver dentro de los permisos; FAILED ante un fallo no recuperable. Continúa entre pasos reversibles y rutinarios sin pedir confirmación. Respeta las acciones prohibidas y devuelve únicamente el resultado conforme al schema.'
fi

ARGS=(
    exec
    --profile hermes
    --sandbox workspace-write
    --output-schema "$SCHEMA_FILE"
    --output-last-message "$RESULT_FILE"
    --add-dir "$BRAIN_DIR"
)
if [[ -d "$QA_DIR" ]]; then
    ARGS+=(--add-dir "$QA_DIR")
fi

set +e
if (( SMOKE_TEST )); then
    ARGS+=(--config sandbox_workspace_write.network_access=false)
fi
cd "$WORKING_DIRECTORY"
if [[ -n "$TIMEOUT_SECONDS" ]]; then
    if ! [[ "$TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]]; then
        echo "ERROR: timeout invÃ¡lido" >&2
        exit 2
    fi
    timeout --foreground "$TIMEOUT_SECONDS" codex "${ARGS[@]}" "$BOOTSTRAP" "${PASSTHROUGH_ARGS[@]}" 2>&1 | tee "$LOG_FILE"
else
    codex "${ARGS[@]}" "$BOOTSTRAP" "${PASSTHROUGH_ARGS[@]}" 2>&1 | tee "$LOG_FILE"
fi
CODEX_STATUS="${PIPESTATUS[0]}"
set -e
echo "Hermes exec log: $LOG_FILE"
echo "Hermes exec result: $RESULT_FILE"

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

if not isinstance(result, dict) or not required.issubset(result):
    print("ERROR: result.json no cumple la estructura mínima de Hermes", file=sys.stderr)
    raise SystemExit(1)
if result["status"] not in allowed_statuses:
    print("ERROR: status inválido en result.json", file=sys.stderr)
    raise SystemExit(1)
if not isinstance(result["summary"], str) or not isinstance(result["completed"], list):
    print("ERROR: tipos inválidos en result.json", file=sys.stderr)
    raise SystemExit(1)
if not isinstance(result["remaining"], list) or not isinstance(result["evidence"], list):
    print("ERROR: listas inválidas en result.json", file=sys.stderr)
    raise SystemExit(1)
if result["gate"] is not None and not isinstance(result["gate"], str):
    print("ERROR: gate inválido en result.json", file=sys.stderr)
    raise SystemExit(1)
if smoke_test and result["status"] != "DONE":
    print("ERROR: el smoke test no ha devuelto status DONE", file=sys.stderr)
    raise SystemExit(1)
PY
then
    VALIDATION_STATUS=1
fi

if (( CODEX_STATUS != 0 )); then
    exit "$CODEX_STATUS"
fi
exit "$VALIDATION_STATUS"
