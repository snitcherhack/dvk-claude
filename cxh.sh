#!/usr/bin/env bash

# Hermes interactivo: conserva el cwd y usa la tarea activa del brain.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
HOME_DIR="${HOME:?HOME no está definido}"
QA_DIR="${WINNER_TIMELINE_QA_DIR:-$HOME_DIR/winner-timeline-portrait-qa}"

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
if [[ ! -f "$TASK_FILE" ]]; then
    echo "ERROR: no existe la TAREA_ACTIVA: $TASK_FILE" >&2
    exit 1
fi

for arg in "$@"; do
    if [[ "$arg" == "--dry-run" ]]; then
        echo "cwd: $(pwd)"
        echo "brain: $BRAIN_DIR"
        echo "task: $TASK_FILE"
        echo "qa: $QA_DIR"
        echo "command: codex --profile hermes --sandbox workspace-write --ask-for-approval on-request --add-dir $BRAIN_DIR --add-dir $QA_DIR <bootstrap>"
        exit 0
    fi
done

BOOTSTRAP='Lee las instrucciones del repositorio y la TAREA_ACTIVA.md de Hermes. Ejecuta autónomamente el objetivo activo y deja evidencia. Continúa entre pasos reversibles y rutinarios sin pedir confirmación. Detente únicamente ante un human_gate, un bloqueo real o una success_condition. Respeta todas las acciones prohibidas de la tarea.'

ARGS=(
    --profile hermes
    --sandbox workspace-write
    --ask-for-approval on-request
    --add-dir "$BRAIN_DIR"
)
if [[ -d "$QA_DIR" ]]; then
    ARGS+=(--add-dir "$QA_DIR")
fi

echo "Lanzando Hermes interactivo..."
exec codex "${ARGS[@]}" "$BOOTSTRAP" "$@"
