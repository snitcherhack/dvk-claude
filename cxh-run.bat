@echo off
setlocal EnableExtensions

set "SCRIPT_DIR=%~dp0"
set "QA_DIR=%WINNER_TIMELINE_QA_DIR%"
if not defined QA_DIR set "QA_DIR=%USERPROFILE%\winner-timeline-portrait-qa"
set "STATE_DIR=%LOCALAPPDATA%\hermes"
if defined XDG_STATE_HOME set "STATE_DIR=%XDG_STATE_HOME%\hermes"
set "SCHEMA_FILE=%SCRIPT_DIR%hermes-run-result.schema.json"

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
    echo state/log: %STATE_DIR%
    echo schema: %SCHEMA_FILE%
    echo command: codex exec --profile hermes --sandbox workspace-write --output-schema %SCHEMA_FILE% --output-last-message ^<result^> ^<bootstrap^>
    exit /b 0
)

if not exist "%STATE_DIR%" mkdir "%STATE_DIR%"
for /f "tokens=1-4 delims=/:. " %%a in ("%date% %time%") do set "RUN_ID=%%d%%b%%c-%%a%%e%%f"
set "RUN_DIR=%STATE_DIR%\%RUN_ID%"
mkdir "%RUN_DIR%" >nul 2>&1
set "LOG_FILE=%RUN_DIR%\codex-exec.log"
set "RESULT_FILE=%RUN_DIR%\result.json"
set "BOOTSTRAP=Lee las instrucciones del repositorio y la TAREA_ACTIVA.md de Hermes. NO finalices simplemente porque terminaste un subpaso. Continua ejecutando la TAREA_ACTIVA hasta que ocurra exactamente uno: DONE cuando todas las success_conditions se hayan alcanzado; WAIT_USER cuando se alcance un human_gate; BLOCKED cuando exista un bloqueo tecnico real que no puedas resolver dentro de los permisos; FAILED ante un fallo no recuperable. Continua entre pasos reversibles y rutinarios sin pedir confirmacion. Respeta las acciones prohibidas y devuelve unicamente el resultado conforme al schema."

echo Lanzando Hermes exec; log: %LOG_FILE%
call codex exec --profile hermes --sandbox workspace-write --output-schema "%SCHEMA_FILE%" --output-last-message "%RESULT_FILE%" --add-dir "%BRAIN_DIR%" --add-dir "%QA_DIR%" "%BOOTSTRAP%" %* > "%LOG_FILE%" 2>&1
set "CODEX_STATUS=%ERRORLEVEL%"
type "%LOG_FILE%"
echo Hermes exec result: %RESULT_FILE%
exit /b %CODEX_STATUS%
