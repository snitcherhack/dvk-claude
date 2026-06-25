#!/bin/bash
# prodanger.sh - Aplica settings Linux y lanza Claude Code sin confirmaciones
SCRIPT_DIR="$(dirname "$0")"

echo "Aplicando settings Linux..."
cp "$SCRIPT_DIR/settings/settings.linux.json" "$HOME/.claude/settings.json"

unset ANTHROPIC_BASE_URL
unset ANTHROPIC_API_KEY
unset ANTHROPIC_AUTH_TOKEN
unset ANTHROPIC_CLIENT_MODE
unset CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY

echo "Lanzando Claude Code (Pro - danger)..."
claude --dangerously-skip-permissions
