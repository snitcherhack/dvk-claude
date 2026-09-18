# Hermes: integración Claude Code + Codex

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
      "codex": {"kind": "cxh-run", "...": "..."},
      "claude": {"kind": "claude-agent", "...": "..."}
    }

El tipo `claude-agent` ya está implementado de forma fail-closed y con carga
perezosa del SDK. Puede construirse aunque el SDK aún no esté instalado; una
ejecución real queda `BLOCKED` hasta que `main-linux` tenga Claude Agent SDK y
la sesión de Claude disponible.

Perfiles iniciales:

- `claude_smoke`: solo `Read`, `Glob` y `Grep`.
- `hermes`: añade `Write` y `Edit`, pero mantiene `Bash` y herramientas de red
  deshabilitadas hasta disponer de una política específica para comandos.

Todas las herramientas de ruta pasan por un hook `PreToolUse` que deniega
accesos fuera de `authorized_roots`. La configuración del SDK usa
`setting_sources=[]` y `skills=[]` para aislar el worker de ajustes locales no
declarados. Con `subscription_only=true`, la presencia de `ANTHROPIC_API_KEY`
o de los selectores `CLAUDE_CODE_USE_BEDROCK`, `CLAUDE_CODE_USE_VERTEX` o
`CLAUDE_CODE_USE_FOUNDRY` bloquea la ejecución para evitar desviar el consumo a
API o proveedores cloud. No se bloquean credenciales OAuth de la suscripción.

En modo de suscripción, el adapter requiere además un `cli_path` explícito hacia
un Claude Code ya autenticado. El SDK incluye su propio CLI, pero no se asume
que comparta la sesión OAuth del CLI interactivo del usuario. La configuración
del worker expone ese binario mediante `cli_path_env`.

El adapter exige `claude-agent-sdk >= 0.2.140`; la versión objetivo para la
primera validación real será `0.2.156`, publicada el 18-09-2026. El mínimo se
fija porque necesitamos hooks `PreToolUse`, `dontAsk`, `setting_sources=[]` y
structured output en el flujo headless.

## Contrato de resultados

Los workers nuevos devuelven un sobre neutral:

    "engine": "claude",
    "engine_result": { ... contrato Hermes ... }

El Controller sigue aceptando temporalmente el campo histórico `codex_result`
para no romper workers antiguos. Un sobre no puede contener ambos formatos a
la vez.

## Papel de codex-plugin-cc

`codex-plugin-cc` no forma parte del núcleo del Controller. Se reserva para el
modo `hybrid`, donde Claude puede solicitar a Codex revisión, rescate o
delegación. Las tareas `codex` siguen yendo directamente al adapter de Codex.

## Secuencia de implementación

1. Routing multi-engine y validación de capabilities. HECHO.
2. Mantener Codex como motor por defecto. HECHO.
3. Implementar `ClaudeAgentAdapter` con roots, timeout, hooks y resultado
   estructurado. HECHO en código; pendiente validación con SDK real.
4. Instalar/verificar Claude Code y Claude Agent SDK en `main-linux`.
5. Ejecutar smoke E2E de Claude sin modificar repositorios reales.
6. Validar la escritura controlada `Write`/`Edit` en un repositorio desechable
   antes de habilitar tareas Claude de desarrollo reales.
7. Evaluar `codex-plugin-cc` para `hybrid`.
8. Añadir política de selección de motor al Controller solo después de validar
   los tres modos de forma independiente.
