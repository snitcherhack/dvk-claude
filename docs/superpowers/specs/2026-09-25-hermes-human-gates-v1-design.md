# Hermes Human Gates v1 — diseño y estado

Fecha: 2026-09-25

Estado: **Human Gates v1 fusionado, desplegado y validado en producción** (`7f9d8bc`, 2026-09-25).

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

Validación local final antes del E2E: **573 tests passed**, `compileall` y
`git diff --check` correctos.

## E2E distribuido real

Fecha: 2026-09-25. Revisión probada: `cd16ee07ec12d88fac145e3b42ddf078221b8623`.

El despliegue fue temporal y coordinado:

- Controller aislado en `hermes01`, runtime
  `/home/snitcher/.local/state/dvk-hermes-controller-phase13`;
- worker `main-linux-phase13`, state separado bajo
  `/home/deiv/.local/state/dvk-hermes-worker-phase13`;
- checkout temporal del Controller y worktree independiente del worker;
- token de operador efímero separado del token de enrolment;
- repositorio desechable
  `/home/deiv/Proyectos/hermes-phase13-gate-smoke-repo`, HEAD
  `4b4822fc26e345654348bf1b67139174d33219c2`.

El endpoint Tailscale productivo se reutilizó únicamente como transporte hacia
el Controller temporal: los servicios productivos se detuvieron durante el
corte, pero sus runtimes, bases de datos y checkouts no se modificaron.

### Camino APPROVED

Job: `a2240616-ea4c-40cd-a9e2-635ca30203d6`.

1. attempt 1 / run `c47ed073-3868-409e-9944-d0c462f7c9b4` terminó
   `WAIT_USER` con gate `HERMES_PHASE13_E2E_APPROVAL`;
2. el token de worker recibió **HTTP 401** al consultar el API de operador;
3. el token efímero de operador consultó el gate y registró `APPROVED`;
4. la decisión produjo `QUEUED` y un nuevo attempt;
5. attempt 2 / run `a1fbc380-218b-4ac2-b051-d456712a676a` recibió
   `_hermes_gate_context={"approved_gates":["HERMES_PHASE13_E2E_APPROVAL"]}`;
6. `hermes-task-gates.md` contenía el gate aprobado, pero no actor ni nota;
7. Codex devolvió `DONE`, `gate=null`, sin modificar el repositorio.

Estado final: `DONE`, dos runs: `WAIT_USER`, `DONE`.

### Camino REJECTED

Job: `433ddc2d-7985-4202-bff7-b5730c14e4b3`.

1. attempt 1 / run `6462376c-697c-4234-a16a-e7465cf83ff8` terminó
   `WAIT_USER` con el mismo gate;
2. el operador registró `REJECTED`;
3. el job pasó a `CANCELLED`;
4. tras varios ciclos de polling permaneció con **un único run** y nunca fue
   reclamado de nuevo.

### Seguridad e integridad

- token de operador en DB: **no**;
- token raw del worker en DB: **no**;
- ambos tokens en journals temporales: **no**;
- `actor`, `note` y `_hermes_gate_context` en task snapshots persistidos:
  **no**;
- actor/nota permanecen exclusivamente en `gate_decisions`;
- repo desechable: HEAD idéntico y working tree limpio antes y después;
- producción se restauró en `36dbc940467f5544945c0d27314e03e2572d546f`,
  Controller y worker activos, heartbeat fresco y cero jobs productivos activos.

Conclusión: el lifecycle distribuido
`WAIT_USER -> APPROVED -> QUEUED -> nuevo attempt -> DONE` y
`WAIT_USER -> REJECTED -> CANCELLED` está verificado.

## Rollout productivo

Tras el E2E distribuido temporal, la feature se revisó y avanzó por fast-forward
a `main` en `7f9d8bc1426b8fd7e6edeedf4286bd848c77e844`.

Despliegue permanente:

- Controller `hermes01` y worker `main-linux` ejecutan el mismo commit
  `7f9d8bc`;
- el Controller productivo mantiene su SQLite/runtime previo;
- el token de operador se generó de forma aleatoria y se almacena solo en
  `/home/snitcher/.config/dvk-hermes/controller.env` con backup privado previo;
- systemd habilita el API mediante
  `--operator-token-env HERMES_OPERATOR_TOKEN`; el secreto no aparece en la
  unidad ni en Git;
- el worker conserva su configuración productiva y sus tokens previos.

### Smoke post-despliegue

Job: `b1440844-8bcd-4b74-b0b1-7f17cca04595`.

1. attempt 1 / run `11de44bf-3a72-40bf-b6ec-dc4d34ffd919` terminó
   `WAIT_USER` con gate `HERMES_PHASE13_PROD_APPROVAL`;
2. el token productivo del worker recibió HTTP **401** contra
   `GET /v1/gates/<job>`;
3. el token productivo de operador registró `APPROVED`, disposition
   `QUEUED`;
4. attempt 2 / run `8077b338-2886-4db7-a31f-decc9749f07e` recibió solo el
   gate aprobado en `_hermes_gate_context`;
5. el job terminó `DONE`, `gate=null`, sin modificar el repositorio.

Comprobaciones adicionales:

- operator token en DB: no;
- worker token en DB: no;
- metadatos humanos `actor/note/decided_at/source_run_id/disposition` en el
  task persistido: no;
- `_hermes_gate_context` en el task persistido: no;
- `hermes-task-gates.md` contiene únicamente las instrucciones y el nombre del
  gate aprobado, sin actor ni nota;
- repo desechable: HEAD idéntico y working tree limpio;
- proyecto temporal retirado del registry tras el smoke.

Conclusión: Human Gates v1 queda **operativo en producción**. El siguiente
trabajo es integrar Telegram/gateway con el API de operador; Telegram no debe
acceder directamente a SQLite ni reutilizar tokens de worker.
