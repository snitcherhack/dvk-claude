# Hermes Telegram Human Gates v1 — diseño y estado

Fecha: 2026-09-26

Estado: **implementación local en dos worktrees; pendiente de validación final,
commit y E2E distribuido con Telegram real**.

## Objetivo

Conectar el lifecycle productivo de Human Gates con Telegram sin desplazar la
autoridad fuera del Controller:

```text
WAIT_USER
  -> Controller operator API
  -> Telegram gateway
  -> botón Aprobar/Rechazar
  -> Controller operator API
  -> QUEUED / CANCELLED / NEEDS_RECONCILIATION
```

Telegram es una interfaz humana. No lee SQLite del Controller, no decide el
estado del job y no reutiliza credenciales de worker.

## Estado auditado antes de implementar

### Controller

Repo: `dvk-claude`

Base de Fase 14:

`4cb003a7f5889a4403c9cbc8ce8f0eff6055130b`

Human Gates v1 ya está operativo en producción y expone:

- `GET /v1/gates/<job_id>`;
- `POST /v1/gates/approve`;
- `POST /v1/gates/reject`.

El API usa `HERMES_OPERATOR_TOKEN`, separado del token del worker.

Huecos confirmados antes de Fase 14:

1. no existía listado de gates pendientes;
2. approve/reject no quedaban ligados al `source_run_id` concreto;
3. conflictos/callbacks caducados devolvían HTTP 400;
4. el schema publicado de `human_gates` conservaba un enum fijo de siete
   nombres aunque el Controller runtime ya aceptaba gates adicionales.

### Gateway

Repo separado:

`/home/snitcher/.hermes/hermes-agent`

Upstream:

`NousResearch/hermes-agent`

Base auditada:

`bfcab25dcdb07e639b72cdabe473cbb42edad241`

Estado:

- checkout productivo `main` en `bfcab25d`, working tree limpio;
- el `origin/main` upstream ha avanzado de forma masiva respecto a esa revisión; Fase 14 no incluye actualizar/rebasar Hermes Agent ni mezclar ese upgrade con la integración DVK;
- la feature del gateway parte exactamente de la revisión productiva actual para aislar el cambio;
- `hermes-gateway.service` activo;
- Telegram usa `python-telegram-bot 22.6`;
- ya existen `InlineKeyboardMarkup`, `CallbackQueryHandler` y callbacks
  compactos para aprobaciones internas;
- esos mappings existentes son solo memoria y no son apropiados como estado
  durable de Human Gates.

Hallazgo de seguridad:

`GATEWAY_ALLOW_ALL_USERS=true` está habilitado actualmente. Por ello Human
Gates **no puede reutilizar la autorización genérica del gateway**.

## Worktrees de implementación

Controller:

- rama: `feat/telegram-human-gates-v1`;
- worktree: `/home/deiv/Proyectos/dvk-claude-telegram-gates-v1`;
- base: `4cb003a`.

Gateway:

- rama local: `feat/dvk-human-gates-telegram-v1`;
- worktree: `/home/snitcher/Proyectos/hermes-agent-human-gates-v1`;
- base: `bfcab25d`;
- esta rama no se publicará en el remoto de NousResearch sin un gate humano
  explícito.

## Contrato Controller v1

### Listado pendiente

Nueva ruta de operador:

```text
GET /v1/gates/pending
```

Respuesta:

```json
{
  "gates": [
    {
      "job_id": "...",
      "project": "...",
      "gate": "...",
      "source_run_id": "...",
      "attempt": 1,
      "summary": "..."
    }
  ]
}
```

No expone task text, rutas, lease, tokens, actor ni notas.

### Resolución ligada a la espera

`POST /v1/gates/approve` y `POST /v1/gates/reject` admiten:

```json
{
  "job_id": "...",
  "gate": "...",
  "source_run_id": "...",
  "actor": "telegram:<user_id>"
}
```

`source_run_id` es opcional para compatibilidad con clientes locales
existentes, pero el bridge Telegram lo envía siempre.

Si se suministra y no coincide con la espera actual, la resolución falla
cerrado.

### HTTP 409

Los conflictos temporales/semánticos de un gate se separan de una petición mal
formada:

- gate ya resuelto de forma contraria -> 409;
- job ya no está en `WAIT_USER` -> 409;
- gate ya no es el actual -> 409;
- `source_run_id` no coincide -> 409.

Inputs inválidos siguen en 400.

### Nombres de gates

Controller y schema se alinean en:

```text
^[A-Z][A-Z0-9_]{0,127}$
```

La lista debe contener nombres únicos. Se elimina el enum histórico fijo.

Esto permite gates de prueba como:

`HERMES_PHASE14_TELEGRAM_TEST`

sin relajar el formato a texto arbitrario.

## Bridge del gateway

Módulo aislado:

`gateway/hermes_gates.py`

`telegram.py` solo:

1. arranca/para el bridge;
2. renderiza mensajes y botones;
3. enruta callbacks `hg:*`.

El bridge está deshabilitado por defecto y requiere:

`HERMES_GATE_BRIDGE_ENABLED=true`

### Configuración

Variables previstas:

- `HERMES_GATE_CONTROLLER_URL`
- `HERMES_OPERATOR_TOKEN`
- `HERMES_GATE_APPROVER_USERS`
- `HERMES_GATE_APPROVER_CHATS`
- `HERMES_GATE_ACTIONABLE_GATES`
- `HERMES_GATE_STATE_DB`
- `HERMES_GATE_POLL_INTERVAL_SECONDS`
- `HERMES_GATE_CALLBACK_TTL_SECONDS`

Si no hay chats configurados, el bridge no arranca.

Si no hay usuarios aprobadores o el gate no está en
`HERMES_GATE_ACTIONABLE_GATES`, el mensaje es informativo y no tiene botones.

`GATEWAY_ALLOW_ALL_USERS` y las autorizaciones generales del bot no conceden
ningún permiso de Human Gates.

### Callback Telegram

Formato:

```text
hg:a:<opaque-id>
hg:r:<opaque-id>
```

Los IDs son aleatorios y caben holgadamente en el límite de 64 bytes de
Telegram.

Nunca se introducen `job_id`, gate, token ni `source_run_id` dentro de
`callback_data`.

### Estado local durable

DB separada, por defecto:

`~/.hermes/human-gates-telegram.db`

Permisos del fichero: 0600.

Solo persiste routing:

- opaque_id;
- job_id;
- gate;
- source_run_id;
- chat_id;
- message_id;
- estado;
- created_at;
- expires_at.

No persiste:

- operator token;
- worker token;
- actor;
- notas humanas;
- task content.

Estados:

```text
PENDING
  -> IN_FLIGHT
      -> RESOLVED
      -> STALE
      -> AUTH_FAILED
      -> PENDING       (error de red/retry)

PENDING -> EXPIRED
INFO
```

Un restart recupera `IN_FLIGHT -> PENDING`; la idempotencia y
`source_run_id` del Controller hacen seguro el reintento.

### Reconciliación

Polling exitoso de `/v1/gates/pending`:

- crea un único prompt activo por `source_run_id + chat_id`;
- no repite mensajes tras reiniciar;
- si una espera desaparece porque fue resuelta por CLI/u otra vía, el mensaje
  se marca `STALE` y pierde botones;
- un callback expirado no llama al Controller y el siguiente poll puede crear
  un prompt fresco.

### Errores

- 200: marcar `RESOLVED` y retirar botones;
- 409: consultar el estado del job, marcar `STALE` y retirar botones;
- 401: marcar `AUTH_FAILED` y detener polling fail-closed;
- error de red: volver a `PENDING` para permitir retry humano.

## Autorización dedicada

Un botón solo es accionable cuando coinciden:

1. el `user_id` de Telegram está en `HERMES_GATE_APPROVER_USERS`;
2. el `chat_id` está en `HERMES_GATE_APPROVER_CHATS`;
3. el gate está en `HERMES_GATE_ACTIONABLE_GATES`.

Las tres condiciones son independientes de la autorización normal del bot.

Durante el primer E2E:

```text
HERMES_GATE_ACTIONABLE_GATES=HERMES_PHASE14_TELEGRAM_TEST
```

Los gates reales de alto impacto siguen siendo solo informativos.

## Gestión del secreto en despliegue

No se copiará `HERMES_OPERATOR_TOKEN` a Git, config.yaml, logs ni DB.

Antes del despliegue se preparará `/home/snitcher/.config/dvk-hermes/operator.env`, fichero privado mínimo `chmod 0600`, fuera de repos y consumido por systemd. Contendrá únicamente `HERMES_OPERATOR_TOKEN`. El token se retirará de `controller.env` para no mantener dos copias activas del mismo secreto; Controller y gateway leerán `operator.env`. La CLI local continúa usando el Controller directamente y no necesita ese token HTTP.

La configuración no secreta del bridge (`HERMES_GATE_BRIDGE_ENABLED`, URL, allow-lists, gate accionable, state DB, intervalos) irá en un drop-in/env privado separado del gateway. El token del worker nunca se entrega al gateway.

## E2E previsto

Repo y proyecto desechables. Gate único:

`HERMES_PHASE14_TELEGRAM_TEST`

Casos:

1. WAIT_USER -> mensaje Telegram -> Aprobar -> attempt siguiente -> DONE;
2. WAIT_USER -> mensaje Telegram -> Rechazar -> CANCELLED;
3. doble toque -> una sola decisión;
4. resolución previa por CLI -> botón antiguo -> 409/STALE;
5. usuario no autorizado -> sin llamada al Controller;
6. chat no autorizado -> sin llamada al Controller;
7. worker token contra operator API -> 401;
8. restart del gateway con prompt pendiente -> sin duplicado y callback útil;
9. operator token inválido -> bridge fail-closed.

## Validación pre-E2E

Estado local verificado el 26 de septiembre de 2026:

- Controller feature `65a7379`: `18/18` tests dirigidos de Human Gates y
  **577 tests** globales; `compileall` y `git diff --check` PASS.
- Gateway feature `5fdbfc1e`: `16/16` tests dirigidos del bridge y
  **446 tests** Telegram/gateway; `compileall` y `git diff --check` PASS.
- Smoke HTTP real del bridge contra un servidor local temporal: PASS;
  `GET /pending -> callback hg:a:<opaque-id> -> POST approve` transportó el
  `source_run_id`, usó actor `telegram:<user_id>`, recibió
  `APPROVED/QUEUED`, persistió `RESOLVED` y actualizó el prompt.
- Producción no tiene `HERMES_GATE_BRIDGE_ENABLED`; desplegar el código del
  gateway sin la configuración nueva no activa polling ni botones de gates.

## Gates antes de producción

Completado:

1. diseño;
2. implementación local en ambos repos;
3. validación estática, suites ampliadas y smoke HTTP sin Telegram real.

Pendiente:

1. identificar/autorizar de forma explícita los Telegram user IDs y chat IDs
   que podrán aprobar el gate ficticio del E2E;
2. aprobación humana del despliegue temporal/E2E;
3. E2E real con Telegram;
4. rollback del despliegue temporal y revisión de evidencia;
5. revisión humana antes de merge/push del Controller y antes de modificar
   permanentemente el checkout/servicio productivo del gateway.

No se habilitarán gates de publicación, render, infraestructura o pentest en
Telegram durante esta fase.
