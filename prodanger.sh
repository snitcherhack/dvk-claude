#!/bin/bash
# prodanger.sh - Lanza Claude Code con suscripción Anthropic, modo dangerous (Linux/Ubuntu)
# Salta confirmaciones de permisos. Usar en PC trabajo (Ubuntu). Windows usa prodanger.bat

# Limpia variables del proxy free-claude-code por si están activas
unset ANTHROPIC_BASE_URL
unset ANTHROPIC_API_KEY
unset ANTHROPIC_AUTH_TOKEN
unset ANTHROPIC_CLIENT_MODE
unset CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY

echo "Lanzando Claude Code (Pro/suscripción) en modo dangerous..."
claude --dangerously-skip-permissions
