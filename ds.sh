#!/bin/bash
# ~/free-claude-code/ds.sh - Arranca proxy + lanza Claude Code con DeepSeek (Linux)
# Usar en el PC de trabajo (Ubuntu). Windows usa ds.bat

cd "$(dirname "$0")"

echo "Matando proxy anterior..."
pkill -f "uvicorn" 2>/dev/null || true
sleep 1

echo "Arrancando proxy free-claude-code..."
nohup uv run uvicorn server:app --host 0.0.0.0 --port 8082 > /tmp/fcc-proxy.log 2>&1 &
sleep 4

export ANTHROPIC_BASE_URL="http://localhost:8082"
export ANTHROPIC_API_KEY="freecc"
export ANTHROPIC_CLIENT_MODE="api-key"
export CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1

echo "Lanzando Claude Code con DeepSeek..."
claude

echo "Limpiando proxy..."
kill %1 2>/dev/null || true
