# Hermes: integración Claude Code + Codex

> Flujo completo Telegram/Director -> Controller -> worker -> engines: docs/hermes-communication-architecture.md.

## Objetivo

Hermes conserva la orquestación en el Controller y permite que cada tarea declare
un motor de ejecución. El worker selecciona el adapter correspondiente; Claude
Code y Codex no controlan la cola, las leases ni el estado global.

## Contrato de tarea

`execution_engine` es opcional para mantener compatibilidad con tareas existentes.
Si falta, una tarea que requiera `codex` se interpreta como `codex`; las demás
se consideran `native`. Las nuevas tareas Claude/híbridas deben declararlo de
forma explícita.

Valores reservados:

- `codex`: ejecución directa con Codex.
- `claude`: ejecución con Claude Agent SDK.
- `hybrid`: flujo combinado Claude + Codex.
- `native`: ejecución no-LLM propia del worker, por ejemplo render/QA de Windows.

Cuando se declara el motor explícitamente, `required_capabilities` debe contener:

- `codex` -> `codex`
- `claude` -> `claude`
- `hybrid` -> `codex` y `claude`
- `native` -> las capabilities específicas de la tarea

Esto impide que un worker reclame una tarea para un motor que no anuncia.

## Worker multi-engine

El worker admite la configuración histórica `adapter` y, de forma compatible,
una nueva configuración `adapters` con `default_execution_engine`.

Ejemplo conceptual:

    "default_execution_engine": "codex",
    "adapters": {
      "codex": {"kind": "codex-run", "...": "..."},
      "claude": {"kind": "claude-agent", "...": "..."}
    }


## Codex project-agnostic

El runner genérico `hermes-codex-run.sh` elimina la dependencia operativa de
`brain/proyectos/youtube/TAREA_ACTIVA.md`. Cada job proporciona explícitamente:

- `working_directory`
- `brain.task_file`
- `run_output_dir`
- `allowed_paths`
- `execution_profile`
- `timeout_seconds`

El adapter `codex-run` valida primero todos esos paths contra los roots máximos
del worker y luego transmite los `allowed_paths` al runner. El runner vuelve a
validar que el cwd, el task file y el output estén dentro de ese scope y solo
expone esos directorios a `codex exec --add-dir`.

`cxh-run.sh` se conserva como compatibilidad con el flujo histórico de Winner
Timeline, pero `main-linux` debe usar `hermes-codex-run.sh` para tareas nuevas.
El smoke genérico crea su repositorio desechable dentro del propio
`run_output_dir`; no depende de `WINNER_TIMELINE_QA_DIR` ni de un proyecto
concreto.

El tipo `claude-agent` ya está implementado de forma fail-closed y con carga
perezosa del SDK. Puede construirse aunque el SDK aún no esté instalado; una
ejecución real queda `BLOCKED` hasta que `main-linux` tenga Claude Agent SDK y
la sesión de Claude disponible.

Perfiles iniciales:

- `claude_smoke`: solo `Read`, `Glob` y `Grep`.
- `hermes`: añade `Write` y `Edit`, pero mantiene `Bash` y herramientas de red
  deshabilitadas hasta disponer de una política específica para comandos.

Todas las herramientas de ruta pasan por un hook `PreToolUse` que deniega
accesos fuera de `authorized_roots`. Cuando Claude Code se ejecuta desde Windows
contra un workspace WSL, sus herramientas pueden reportar rutas UNC del tipo
`\\wsl.localhost\\<distro>\\...`; el adapter las normaliza a rutas POSIX solo
si el nombre de distro coincide con `WSL_DISTRO_NAME`, y rechaza otras rutas
Windows/UNC para evitar escapes de `allowed_paths`. La configuración del SDK usa
`setting_sources=[]` y `skills=[]` para aislar el worker de ajustes locales no
declarados. Con `subscription_only=true`, la presencia de `ANTHROPIC_API_KEY`
o de los selectores `CLAUDE_CODE_USE_BEDROCK`, `CLAUDE_CODE_USE_VERTEX` o
`CLAUDE_CODE_USE_FOUNDRY` bloquea la ejecución para evitar desviar el consumo a
API o proveedores cloud. No se bloquean credenciales OAuth de la suscripción.

En modo de suscripción, el adapter requiere además un `cli_path` explícito hacia
un Claude Code ya autenticado. El SDK incluye su propio CLI, pero no se asume
que comparta la sesión OAuth del CLI interactivo del usuario. La configuración
del worker expone ese binario mediante `cli_path_env`. En `main-linux` se usa el
binario nativo WSL `/home/deiv/.local/bin/claude`, autenticado mediante
`claude.ai`; ya no se depende del ejecutable de Windows para el daemon.

El adapter exige `claude-agent-sdk >= 0.2.140`; la validación real se completó
con `0.2.156`. El mínimo se fija porque necesitamos hooks `PreToolUse`,
`dontAsk`, `setting_sources=[]` y structured output en el flujo headless.

## Contrato de resultados

Los workers nuevos devuelven un sobre neutral:

    "engine": "claude",
    "engine_result": { ... contrato Hermes ... }

El Controller sigue aceptando temporalmente el campo histórico `codex_result`
para no romper workers antiguos. Un sobre no puede contener ambos formatos a
la vez.

## Modo híbrido y papel de codex-plugin-cc

El modo `hybrid` del worker no depende del plugin para la ejecución autónoma.
Compone directamente `ClaudeAgentAdapter` como implementador y `CodexRunAdapter`
como reviewer read-only. El reviewer devuelve `DONE` cuando no quedan hallazgos
materiales y `BLOCKED` cuando hace falta otra pasada. Hermes permite hasta un
número acotado de rondas Claude-fix -> Codex-review y deja trazabilidad en
`hybrid-summary.json`.

`codex-plugin-cc` se mantiene como integración opcional de Claude Code para
revisión, rescue y transfer interactivos. Esto evita que el daemon dependa de
prompts slash o decisiones interactivas del plugin, pero conserva la colaboración
Claude <-> Codex para sesiones humanas y para futuras extensiones controladas.

## Secuencia de implementación

1. Routing multi-engine y validación de capabilities. HECHO.
2. Codex project-agnostic con `codex-run` y smoke distribuido. HECHO.
3. `ClaudeAgentAdapter` con roots, timeout, hooks y resultado estructurado. HECHO.
4. Claude read-only y `Write`/`Edit` sobre repositorio desechable. HECHO.
5. `HybridAdapter`: Claude implementa y Codex revisa en read-only. HECHO; E2E distribuido DONE.
6. Claude Code nativo en WSL. HECHO; OAuth `claude.ai` validado y usado por el worker.
7. `codex-plugin-cc` nativo en WSL. HECHO; setup y review backend E2E completados.
8. Consolidar la feature branch y desplegar la misma versión en Controller y worker. HECHO en `main`.
9. Añadir política automática de selección de motor solo después de estabilizar los tres modos.
