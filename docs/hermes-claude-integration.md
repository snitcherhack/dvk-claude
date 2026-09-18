# Hermes: integración Claude Code + Codex

## Objetivo

Hermes conserva la orquestación en el Controller y permite que cada tarea declare
un motor de ejecución. El worker selecciona el adapter correspondiente; Claude
Code y Codex no controlan la cola, las leases ni el estado global.

## Contrato de tarea

`execution_engine` es opcional para mantener compatibilidad con tareas existentes.

Valores reservados:

- `codex`: ejecución directa con Codex.
- `claude`: ejecución con Claude Agent SDK.
- `hybrid`: flujo combinado Claude + Codex.

Cuando se declara el motor explícitamente, `required_capabilities` debe contener:

- `codex` -> `codex`
- `claude` -> `claude`
- `hybrid` -> `codex` y `claude`

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

El tipo `claude-agent` todavía no está implementado. Se añadirá y validará en
`main-linux`, donde estarán Claude Code, su sesión autenticada y el SDK.

## Papel de codex-plugin-cc

`codex-plugin-cc` no forma parte del núcleo del Controller. Se reserva para el
modo `hybrid`, donde Claude puede solicitar a Codex revisión, rescate o
delegación. Las tareas `codex` siguen yendo directamente al adapter de Codex.

## Secuencia de implementación

1. Routing multi-engine y validación de capabilities.
2. Mantener Codex como motor por defecto.
3. Instalar/verificar Claude Code y Claude Agent SDK en `main-linux`.
4. Implementar `ClaudeAgentAdapter` con roots, timeout y resultado estructurado.
5. Ejecutar smoke E2E de Claude sin modificar repositorios reales.
6. Evaluar `codex-plugin-cc` para `hybrid`.
7. Añadir política de selección de motor al Controller solo después de validar
   los tres modos de forma independiente.
