@echo off
title Claude Code Pro Dangerous Launcher

findstr /C:"RUTA_NODE_WINDOWS" "%~dp0settings\settings.windows.json" >nul
if %errorlevel%==0 (
    echo.
    echo [ERROR] settings\settings.windows.json todavia tiene el placeholder RUTA_NODE_WINDOWS sin sustituir.
    echo Ejecuta "where node" en cmd, copia la ruta y sustituyela en ese archivo antes de continuar.
    echo.
    pause
    exit /b 1
)

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
