@echo off
title Claude Code Pro Dangerous Launcher

:: Limpia variables del proxy free-claude-code por si están activas
set ANTHROPIC_BASE_URL=
set ANTHROPIC_API_KEY=
set ANTHROPIC_AUTH_TOKEN=
set ANTHROPIC_CLIENT_MODE=
set CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=

echo Lanzando Claude Code (Pro/suscripcion) en modo dangerous...
claude --dangerously-skip-permissions
pause
