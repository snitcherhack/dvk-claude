# Hermes Human Gates v1 — diseño y estado

Fecha: 2026-09-25

Estado: **implementación local completa; pendiente de E2E distribuido y despliegue productivo**.

## Objetivo

Cerrar el ciclo humano de Hermes sin convertir una aprobación en una acción
implícita ni permitir que workers/modelos decidan gates por sí mismos:

```text
RUNNING
  -> WAIT_USER(gate)
  -> APPROVED -> QUEUED -> nuevo attempt -> RUNNING
  -> REJECTED -> CANCELLED
```

Para tareas `manual_reconcile`, una aprobación produce
`NEEDS_RECONCILIATION` y nunca redistribución automática.

## Autoridad

El Controller es la única autoridad sobre:

- estado del job;
- gate actualmente esperado;
- decisiones humanas;
- actor/nota/audit trail;
- requeue/cancel;
- claims posteriores y contexto interno de gates aprobados.

Los workers y engines solo pueden devolver `WAIT_USER` con un gate declarado en
la tarea.

## Contrato fail-closed

- `WAIT_USER` exige `gate` no nulo y declarado en `task.human_gates`.
- `DONE`, `BLOCKED` y `FAILED` exigen `gate=null`.
- Worker normaliza resultados no conformes antes de ingest.
- Controller vuelve a validar el mismo contrato.
- Un gate aprobado no puede volver a pedirse: Worker y Controller convierten
  ese caso a `BLOCKED`.
- Las claves públicas de task que empiecen por `_hermes_` están reservadas y
  se rechazan al encolar.

## Persistencia

Nueva tabla SQLite `gate_decisions`:

```text
job_id
gate
decision        APPROVED | REJECTED
actor
note
decided_at
source_run_id
disposition     QUEUED | CANCELLED | NEEDS_RECONCILIATION
```

La clave primaria `(job_id, gate)` hace la resolución idempotente y evita dos
decisiones contradictorias para el mismo gate.

La creación usa `CREATE TABLE IF NOT EXISTS`; abrir una DB existente no exige
migrar jobs/runs anteriores.

## Semántica de resolución

### APPROVED + safe_retry

En una única transacción:

1. verifica que el job sigue `WAIT_USER`;
2. verifica que el gate es el actual y está declarado;
3. registra `APPROVED`;
4. cambia el job a `QUEUED`;
5. registra eventos `GATE_APPROVED` y `JOB_RESUMED`.

El siguiente claim crea un nuevo attempt.

### APPROVED + manual_reconcile

Registra la aprobación, pero el job pasa a `NEEDS_RECONCILIATION`.
No hay ejecución automática de una tarea potencialmente no idempotente.

### REJECTED

Registra `REJECTED` y cambia el job a `CANCELLED`.
No puede volver a reclamarse.

## Contexto entregado al worker

El task persistido no se modifica con datos humanos.

Al reclamar un job con gates aprobados, el Controller añade únicamente:

```json
{
  "_hermes_gate_context": {
    "approved_gates": ["GATE_NAME"]
  }
}
```

No incluye actor, nota, timestamps, lease ni credenciales.

El worker valida esos nombres contra `human_gates` y crea
`hermes-task-gates.md` dentro de `run_output_dir`, fuera del repositorio. El
fichero indica que solo los gates nombrados están aprobados y que cualquier otra
decisión humana debe volver a `WAIT_USER`.

## CLI local

```text
gate status  <job_id>
gate approve <job_id> <gate> --actor <actor> [--note ...]
gate reject  <job_id> <gate> --actor <actor> [--note ...]
```

La aprobación y la reanudación son atómicas para `safe_retry`; no existe un
`resume` genérico que pueda redistribuir una tarea no idempotente.

## API de operador

Rutas:

```text
GET  /v1/gates/<job_id>
POST /v1/gates/approve
POST /v1/gates/reject
```

Autenticación:

- usa un bearer token de operador separado de los tokens de worker;
- el token solo vive en runtime/configuración privada;
- el worker token no puede consultar ni resolver gates;
- si no se configura `operator_token`, el API de gates queda inaccesible;
- `serve --operator-token-env ENV_VAR` habilita el canal sin persistir el
  secreto en Git/SQLite/status.

Telegram/gateway deberá usar este canal en una fase posterior. No debe usar
SQLite directamente ni reutilizar un token del worker.

## Pruebas locales

Cubiertas:

- WAIT_USER + gate declarado;
- rechazo de gate ausente/no declarado;
- APPROVED -> QUEUED -> attempt siguiente;
- REJECTED -> CANCELLED;
- decisión idempotente y conflicto rechazado;
- resoluciones concurrentes atómicas;
- múltiples gates secuenciales;
- manual_reconcile -> NEEDS_RECONCILIATION;
- reserva de `_hermes_*`;
- re-request de gate aprobado -> BLOCKED;
- materialización de contexto sin actor/nota;
- E2E HTTP Controller + Worker falso: WAIT_USER -> approve -> DONE;
- CLI approve/status;
- API operador con auth separada;
- worker token no puede usar API de gates;
- API deshabilitada sin operator token.

Checkpoint local: 573 tests globales antes de añadir el API de operador; la
suite final se ejecutará antes del commit.

## Pendiente para cerrar Fase 13

1. suite completa + compileall + diff-check;
2. commit/push de la feature;
3. aprobación humana para despliegue temporal coordinado;
4. E2E distribuido real con un gate inocuo;
5. rollback del despliegue temporal;
6. documentación del resultado.

La conexión con Telegram pertenece a la fase siguiente.
