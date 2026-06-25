@echo off
title DeepSeek Claude Code Launcher

echo Arrancando proxy free-claude-code...
start "Free Claude Code Proxy" cmd /k "cd /d A:\PROYECTOS\free-claude-code\proxy && uv run server.py"

echo Esperando a que el proxy arranque...
timeout /t 4 /nobreak >nul

set ANTHROPIC_BASE_URL=http://localhost:8082
set ANTHROPIC_AUTH_TOKEN=freecc
set CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1

echo Lanzando Claude Code con DeepSeek...
claude
pause