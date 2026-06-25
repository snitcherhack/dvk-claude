#!/bin/bash
# pro.sh - Aplica settings Linux y lanza Claude Code con suscripción
SCRIPT_DIR="$(dirname "$0")"

echo "Aplicando settings Linux..."
cp "$SCRIPT_DIR/settings/settings.linux.json" "$HOME/.claude/settings.json"

# Limpia variables del proxy free-claude-code por si están activas
unset ANTHROPIC_BASE_URL
unset ANTHROPIC_API_KEY
unset ANTHROPIC_AUTH_TOKEN
unset ANTHROPIC_CLIENT_MODE
unset CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY

echo "Lanzando Claude Code (Pro)..."
claude
