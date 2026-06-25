@echo off
title Claude Code Pro Launcher

echo Aplicando settings Windows...
copy /Y "%~dp0settings\settings.windows.json" "%USERPROFILE%\.claude\settings.json"

:: Limpia variables del proxy free-claude-code por si están activas
set ANTHROPIC_BASE_URL=
set ANTHROPIC_API_KEY=
set ANTHROPIC_AUTH_TOKEN=
set ANTHROPIC_CLIENT_MODE=
set CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=

echo Lanzando Claude Code (Pro)...
claude
pause
