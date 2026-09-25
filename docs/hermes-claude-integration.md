# Hermes: integración Claude Code + Codex

> Flujo completo Telegram/Director -> Controller -> worker -> engines: docs/hermes-communication-architecture.md.

## Objetivo

Hermes conserva la orquestación en el Controller y permite que cada tarea declare
un motor de ejecución. El worker selecciona el adapter correspondiente; Claude
Code y Codex no controlan la cola, las leases ni el estado global.

## Contrato de tarea

`execution_engine` es opcional para mantener compatibilidad con tareas existentes.
Si falta, una tarea que requiera `codex` se interpreta como `codex`; las demás
se consideran `native`. Las nuevas tareas Claude, Hybrid y Brainstorm deben
declararlo de forma explícita.

Valores reservados:

- `codex`: ejecución directa con Codex.
- `claude`: ejecución con Claude Agent SDK.
- `hybrid`: flujo combinado Claude + Codex.
- `brainstorm`: propuestas independientes Claude/Codex, evaluación cruzada y decisión determinista de Hermes.
- `native`: ejecución no-LLM propia del worker, por ejemplo render/QA de Windows.

Cuando se declara el motor explícitamente, `required_capabilities` debe contener:

- `codex` -> `codex`
- `claude` -> `claude`
- `hybrid` -> `codex` y `claude`
- `brainstorm` -> `codex` y `claude`
- `native` -> las capabilities específicas de la tarea

Esto impide que un worker reclame una tarea para un motor que no anuncia.

## Worker multi-engine

El worker admite la configuración histórica `adapter` y, de forma compatible,
una nueva configuración `adapters` con `default_execution_engine`.

Ejemplo conceptual:

    "default_execution_engine": "codex",
    "adapters": {
      "codex": {"kind": "codex-run", "...": "..."},
      "claude": {"kind": "claude-agent", "...": "..."},
      "hybrid": {"kind": "hybrid", "primary_engine": "claude", "review_engine": "codex"},
      "brainstorm": {"kind": "brainstorm", "claude_engine": "claude", "codex_engine": "codex"}
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

Para `execution_profile=brainstorm`, Codex no usa el sandbox read-only genérico. El runner construye un permission profile efímero con el `bwrap` integrado de Codex, `env -i`, red de herramientas aislada y una sonda fail-closed antes de cada llamada. El repositorio y `request/` son read-only, la llamada actual es el único root escribible y `orchestration/`, auth, sesiones, sibling stages y ejecuciones anteriores permanecen ocultos. El structured output solo acepta los cuatro schemas versionados `proposals`, `evaluation`, `refinement` y `validation`.

El tipo `claude-agent` ya está implementado de forma fail-closed y con carga
perezosa del SDK. Puede construirse aunque el SDK aún no esté instalado; una
ejecución real queda `BLOCKED` hasta que `main-linux` tenga Claude Agent SDK y
la sesión de Claude disponible.

Perfiles iniciales:

- `claude_smoke`: solo `Read`, `Glob` y `Grep`.
- `hermes`: añade `Write` y `Edit`, pero mantiene `Bash` y herramientas de red
  deshabilitadas hasta disponer de una política específica para comandos.
- `brainstorm`: structured output y solo `Read`, `Glob` y `Grep`; cada etapa recibe roots mínimos `[repo, request, execution]` y no puede escribir en el proyecto.

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
del worker expone ese binario mediante `cli_path_env`. El servicio productivo
restaurado sigue usando el binario nativo WSL `/home/deiv/.local/bin/claude`
(2.1.267 en la verificación de 2026-09-24). Para los smokes Brainstorm y el E2E
distribuido se fijó explícitamente el CLI empaquetado por el SDK,
`/home/deiv/.local/share/dvk-hermes/python/claude_agent_sdk/_bundled/claude`
(2.1.276), evitando cualquier wrapper de Windows o resolución ambigua de
`PATH`. Ambos usan la sesión `claude.ai`; el daemon no depende del ejecutable de
Windows.

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

## Contexto de human gates

Los adapters no reciben identidad del aprobador, notas ni credenciales. Cuando
el Controller reanuda un job `safe_retry` tras una aprobación, el claim lleva
solo los nombres de gates ya aprobados en el contexto interno
`_hermes_gate_context`. El worker valida ese contexto contra
`task.human_gates`, crea `hermes-task-gates.md` bajo `run_output_dir` y
añade instrucciones explícitas: no volver a pedir un gate ya aprobado, no
inferir aprobación de otros gates y devolver `WAIT_USER` para cualquier nueva
decisión humana necesaria.

Ese fichero permanece fuera del checkout del proyecto y reutiliza las mismas
fronteras de materialización que los inline tasks. Si un adapter vuelve a pedir
un gate ya aprobado, Worker y Controller fallan cerrado y normalizan el
resultado a `BLOCKED`.

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

## Modo Brainstorm

`BrainstormAdapter` compone los adapters estructurados reales de Claude y Codex,
pero Hermes conserva toda la coordinación. El flujo normal realiza seis llamadas:

```text
Codex proposals
Claude proposals
Claude evaluation
Codex evaluation
winner-author refinement
other-engine validation
```

Las propuestas se generan de forma independiente; las evaluaciones consumen el
mismo bundle anonimizado. El ranking, desempates, confianza y marca de
autopreferencia se calculan en código determinista. Los outputs válidos se
checkpointan para que un nuevo attempt pueda reutilizarlos sin repetir llamadas.

Cada stage separa `request/` read-only de
`executions/call-NNNN/`. Codex puede escribir únicamente en la ejecución
actual; Claude mantiene herramientas de lectura. `orchestration/`, otras etapas,
ejecuciones previas, credenciales y sesiones no forman parte de los roots del
modelo.

El E2E distribuido real se verificó el 2026-09-24 con Controller temporal en
`hermes01` y worker temporal `main-linux-phase10`, ambos en `4ffb37f`. Tras la
aprobación humana, Brainstorm v1 se fusionó y desplegó permanentemente. El
primer smoke productivo (`6ac83bfc-2997-4302-9f3c-70a51c623ce9`) detectó un
fallo semántico reproducible: Claude refinement devolvió dos títulos de 126 y
133 caracteres cuando el validador exigía <=120; el retry era válido pero el
prompt no explicitaba el límite. El hotfix `569eb8c` añadió todos los límites
semánticos de refinement al task prompt, con regresión dedicada (558 tests).

El segundo smoke productivo (`0fec7b57-bbf5-442d-9069-2793e8970332`) terminó
`DONE / RECOMMENDED_FOR_PILOT`, con seis llamadas reales, cero reintentos,
tres sondas Codex `PASS` (35 checks cada una), once artefactos verificados y
fingerprint Git idéntico. Una llamada Claude-refinement dirigida sobre los
inputs del fallo original produjo después un título válido de 111 caracteres
con `max_turns=12`.

Post-rollout, se revisaron las otras etapas y se confirmó el mismo riesgo de
desalineación: `proposals`, `evaluation` y `validation` validaban límites
semánticos que no aparecían en sus prompts. El hardening `52832ab` elimina los
límites escritos a mano: los cuatro prompts renderizan sus restricciones desde
`PROPOSAL_TEXT`, `PROPOSAL_LISTS`, `EVALUATION_LISTS`, `REFINEMENT_TEXT`,
`REFINEMENT_LISTS` y `VALIDATION_LISTS` de `brainstorm_core`. Los JSON Schemas
siguen siendo estructurales; la semántica continúa siendo autoridad de los
validators. La suite quedó en 559 tests.

El smoke productivo `20fee417-1944-487f-abac-17580656018d`, con una pregunta
intencionadamente detallada, terminó `DONE / RECOMMENDED_FOR_PILOT` con seis
llamadas, cero retries y `stage_retries={}`. Los prompts reales mostraron los
límites esperados; las tres sondas Codex dieron `PASS` (35 checks), los hashes
de artefactos coincidieron y el fingerprint Git permaneció idéntico. Como el
informe completo superó 32 KiB, también se verificó el fallback
`brainstorm-report.inline.md`. Brainstorm v1 queda operativo en producción en
`52832ab`.

## Secuencia de implementación

1. Routing multi-engine y validación de capabilities. HECHO.
2. Codex project-agnostic con `codex-run` y smoke distribuido. HECHO.
3. `ClaudeAgentAdapter` con roots, timeout, hooks y resultado estructurado. HECHO.
4. Claude read-only y `Write`/`Edit` sobre repositorio desechable. HECHO.
5. `HybridAdapter`: Claude implementa y Codex revisa en read-only. HECHO; E2E distribuido DONE.
6. Claude Code nativo en WSL. HECHO; OAuth `claude.ai` validado y usado por el worker.
7. `codex-plugin-cc` nativo en WSL. HECHO; setup y review backend E2E completados.
8. Consolidación previa multi-engine en `main`. HECHO.
9. Brainstorm v1 explicit-only: contrato, core, aislamiento Claude/Codex, adapter y worker multi-engine. HECHO.
10. Integración local con fakes y smoke real local Claude+Codex. HECHO.
11. E2E distribuido Brainstorm `hermes01 -> main-linux -> hermes01`. HECHO.
12. Merge/push, despliegue permanente y smoke productivo de Brainstorm v1. HECHO.
12b. Hardening post-rollout: límites de todas las etapas derivados de `brainstorm_core`, smoke productivo PASS en `52832ab`. HECHO.
13. Cualquier selección automática de Brainstorm o `balanced-v2`. FUERA DE v1 / APLAZADO.
