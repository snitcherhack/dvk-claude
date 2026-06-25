@echo off
title Claude Code Pro Dangerous Launcher

echo Aplicando settings Windows...
copy /Y "%~dp0settings\settings.windows.json" "%USERPROFILE%\.claude\settings.json"

set ANTHROPIC_BASE_URL=
set ANTHROPIC_API_KEY=
set ANTHROPIC_AUTH_TOKEN=
set ANTHROPIC_CLIENT_MODE=
set CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=

echo Lanzando Claude Code (Pro - danger)...
claude --dangerously-skip-permissions
pause
