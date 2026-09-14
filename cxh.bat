@echo off
setlocal EnableExtensions

set "SCRIPT_DIR=%~dp0"
set "QA_DIR=%WINNER_TIMELINE_QA_DIR%"
if not defined QA_DIR set "QA_DIR=%USERPROFILE%\winner-timeline-portrait-qa"

if defined HERMES_BRAIN_DIR if exist "%HERMES_BRAIN_DIR%\proyectos\youtube\TAREA_ACTIVA.md" set "BRAIN_DIR=%HERMES_BRAIN_DIR%"
if not defined BRAIN_DIR if exist "%SCRIPT_DIR%..\brain\proyectos\youtube\TAREA_ACTIVA.md" set "BRAIN_DIR=%SCRIPT_DIR%..\brain"
if not defined BRAIN_DIR if exist "%USERPROFILE%\Proyectos\brain\proyectos\youtube\TAREA_ACTIVA.md" set "BRAIN_DIR=%USERPROFILE%\Proyectos\brain"
if not defined BRAIN_DIR (
    echo ERROR: no se ha podido localizar el brain.
    exit /b 1
)
set "TASK_FILE=%BRAIN_DIR%\proyectos\youtube\TAREA_ACTIVA.md"

if /I "%~1"=="--dry-run" (
    echo cwd: %CD%
    echo brain: %BRAIN_DIR%
    echo task: %TASK_FILE%
    echo qa: %QA_DIR%
    echo command: codex --profile hermes --sandbox workspace-write --ask-for-approval on-request --add-dir %BRAIN_DIR% --add-dir %QA_DIR% ^<bootstrap^>
    exit /b 0
)

set "BOOTSTRAP=Lee las instrucciones del repositorio y la TAREA_ACTIVA.md de Hermes. Ejecuta autonomamente el objetivo activo y deja evidencia. Continua entre pasos reversibles y rutinarios sin pedir confirmacion. Detente unicamente ante un human_gate, un bloqueo real o una success_condition. Respeta todas las acciones prohibidas de la tarea."
echo Lanzando Hermes interactivo...
call codex --profile hermes --sandbox workspace-write --ask-for-approval on-request --add-dir "%BRAIN_DIR%" --add-dir "%QA_DIR%" "%BOOTSTRAP%" %*
exit /b %ERRORLEVEL%
