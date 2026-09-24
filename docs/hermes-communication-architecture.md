# Hermes: arquitectura de comunicación y orquestación

## Objetivo

Este documento define quién habla con quién en Hermes y dónde vive el estado.

- Telegram es una interfaz humana.
- El gateway/Director recibe la conversación.
- `hermes_controller` es la autoridad de proyectos, cola, jobs, runs, leases, resultados y gates.
- Los workers reclaman trabajo del Controller.
- Codex y Claude son motores de ejecución; no hablan directamente entre sí.
- Hybrid es una orquestación de Hermes entre Claude y Codex.
- Brainstorm es una orquestación explícita de Hermes para propuestas independientes, evaluación cruzada, ranking determinista, refinamiento y validación.
- Desktop Commander es un canal administrativo, no el camino normal de los jobs.

## Componentes actuales

| Componente | Ubicación / proceso | Responsabilidad |
| --- | --- | --- |
| Usuario | Telegram o ChatGPT | Formula tareas y toma decisiones humanas |
| Gateway / Director | `hermes01`: `python -m hermes_cli.main gateway run` | Entrada conversacional, incluido Telegram |
| Hermes Controller | `hermes01`: `python3 -m hermes_controller ... serve --host 127.0.0.1 --port 8787` | Registry, task builder, queue, jobs, runs, leases, resultados y gates |
| Estado del Controller | SQLite bajo su runtime | Persistencia de workers, jobs, runs, events y projects |
| `main-linux` | Ubuntu 24.04 WSL2 | Worker general para tareas Linux |
| Codex | ejecutado desde `main-linux` | Implementación, bugs, tests y cambios de código |
| Claude | ejecutado desde `main-linux` | Análisis, arquitectura, investigación y planificación |
| Hybrid | adapter de Hermes | Claude -> Codex review -> posible Claude fix |
| Brainstorm | adapter de Hermes | Codex + Claude proponen y evalúan de forma aislada; Hermes rankea, refina y valida |
| `windows-render` | worker Windows declarado y deshabilitado | Render/QA final cuando se habilite |
| Desktop Commander | canal remoto de administración | Diagnóstico, reparación y mantenimiento |

## Flujo end-to-end

```text
Usuario
  |
  | Telegram
  v
Gateway / Director en hermes01
  |
  | crea o consulta una tarea Hermes
  v
Hermes Controller en hermes01
  |
  | registry + policy + queue
  | persiste job/run/event
  v
Job QUEUED
  ^
  |
  | polling / heartbeat / claim
  |
main-linux
  |
  | execution_engine
  +--------------+--------------+--------------+
  |              |              |              |
  v              v              v              v
Codex          Claude         Hybrid       Brainstorm
  |              |              |              |
  +--------------+--------------+--------------+
                         |
                         | resultado normalizado
                 v
         Hermes Controller
                 |
     DONE / FAILED / BLOCKED / WAIT_USER
                 |
                 v
         Gateway / Director
                 |
                 v
              Telegram
                 |
                 v
              Usuario
```

El Controller no necesita abrir una shell interactiva en `main-linux` para cada tarea. El worker mantiene heartbeat, consulta trabajo disponible, reclama el job compatible con sus capabilities y devuelve el resultado.

## Creación y selección de tareas

Una petición se transforma en un task autocontenido con datos como:

```text
project
repository / ref
working_directory
runtime_directory
allowed_paths
execution_profile
execution_engine
engine_selection
required_capabilities
worker_id
human_gates
timeout_seconds
max_turns
task_text
```

Si el proyecto usa `default_engine=auto`, `balanced-v1` resuelve un motor concreto antes de encolar el job. La decisión queda registrada en `engine_selection`.

La intención actual de `balanced-v1` es:

```text
implementación / bugs / tests              -> Codex
arquitectura / análisis / planificación    -> Claude
review / seguridad / producción /
cambios críticos / migraciones             -> Hybrid
tarea neutra                                -> Codex
```

Una selección explícita `--engine codex|claude|hybrid|native|brainstorm` tiene precedencia, siempre que el proyecto permita ese motor. `brainstorm` es **explicit-only**: puede figurar en `allowed_engines`, pero no puede ser `default_engine` y `balanced-v1` nunca lo selecciona automáticamente.

### Limitación conocida del routing

La regla actual interpreta `review` como Hybrid incluso en auditorías read-only. Una prueba real desde Telegram envió una review read-only a Hybrid y terminó `FAILED` por `error_max_turns`.

La mejora pendiente es separar al menos:

```text
read-only review / audit / inspect          -> Claude
implementation/security/high-impact review  -> Hybrid
```

Hasta entonces, una review puramente analítica puede fijar `engine=claude` explícitamente.

## Comunicación Controller <-> worker

El modelo es polling/claim:

```text
main-linux -> heartbeat -> Controller
main-linux -> claim      -> Controller
Controller -> task       -> main-linux
main-linux -> renew      -> Controller
main-linux -> result     -> Controller
```

Las leases impiden que dos workers posean simultáneamente el mismo run. El Controller es la autoridad de estado; el filesystem del worker no es el estado global de Hermes.

## Cómo funciona Hybrid

Claude y Codex no mantienen una conversación directa. Hermes coordina las fases:

```text
Hermes
  |
  v
Claude: primera fase
  |
  v
Hermes
  |
  v
Codex: review read-only
  |
  +---- DONE ----------------------> Hermes finaliza
  |
  +---- requiere cambios
           |
           v
         Hermes
           |
           v
         Claude: fix round
           |
           v
         Hermes
```

Hybrid es por tanto una state machine de Hermes. El adapter limita las rondas y conserva evidencia del flujo.

## Cómo funciona Brainstorm

Brainstorm también es una state machine de Hermes, pero no sigue la semántica implementación/review/fix de Hybrid. En v1 solo se activa de forma explícita y el repositorio permanece read-only:

```text
Hermes
  |
  +--> Codex: propuestas independientes
  |
  +--> Claude: propuestas independientes
  |
  +--> Hermes: anonimiza candidatas
  |
  +--> Claude: evaluación
  |
  +--> Codex: evaluación
  |
  +--> Hermes: ranking determinista + confianza
  |
  +--> autor de la ganadora: refinement
  |
  +--> otro motor: validation
  |
  +--> Hermes: report + artifacts
```

Los motores no ven la salida del otro durante proposals y ambos evaluadores reciben exactamente el mismo bundle anonimizado. Hermes calcula el ranking y registra el posible sesgo de autopuntuación; los modelos no deciden el orden final.

Una validación final `PASS` produce `DONE / RECOMMENDED_FOR_PILOT`. Una validación `FAIL` produce `DONE / INCONCLUSIVE`; no se cambia automáticamente a la segunda candidata. Construir el piloto siempre requiere una tarea posterior.

Brainstorm v1 superó el 24 de septiembre de 2026 un E2E distribuido real `hermes01 -> main-linux -> Claude/Codex -> hermes01` con seis llamadas reales, cero reintentos, once artefactos verificados y huella Git idéntica. La rama de implementación aún no está fusionada ni desplegada permanentemente: la infraestructura productiva fue restaurada a sus revisiones anteriores tras la prueba.

## Estado compartido

El estado común no vive dentro de Codex ni de Claude. El Controller conserva:

```text
projects
workers
jobs
runs
events
leases
results
```

Cada ejecución recibe una tarea autocontenida y devuelve un resultado normalizado. La superficie pública expone `status`, `summary`, `gate`, `completed`, `remaining`, `evidence`, `artifacts` y `hashes`; no expone lease tokens, credenciales ni el sobre interno completo.

## Human gates

Los proyectos pueden declarar gates como:

```text
INFRASTRUCTURE_APPLY_APPROVAL_REQUIRED
PENTEST_ACTIVE_SCAN_APPROVAL_REQUIRED
YOUTUBE_PUBLICATION_APPROVAL
LICENSE_REVIEW_REQUIRED
WINDOWS_FINAL_RENDER_REQUIRED
VISUAL_ASSET_HANDOFF_REQUIRED
SOCIAL_PUBLICATION_APPROVAL_REQUIRED
```

El comportamiento existente es fail-closed: `WAIT_USER` sólo es válido con un gate declarado por la tarea/proyecto.

La experiencia objetivo es:

```text
engine
  |
  v
WAIT_USER + gate
  |
  v
Controller
  |
  v
Gateway / Telegram: se requiere aprobación
  |
  v
Usuario: aprobar / rechazar
  |
  +---- aprobar ---> Controller reanuda
  |
  +---- rechazar --> Controller cancela/bloquea
```

La resolución y reanudación de gates es una pieza pendiente. Hasta implementarla, `WAIT_USER` es un límite de seguridad, no una aprobación implícita.

## Papel de Telegram

Telegram no es el Controller ni ejecuta Codex o Claude directamente. En `hermes01` existe un gateway separado:

```text
/home/snitcher/.hermes/hermes-agent
python -m hermes_cli.main gateway run
```

Se ha verificado operativamente que una petición enviada por Telegram puede producir un job real en el Controller y recorrer:

```text
queue -> claim main-linux -> engine -> result
```

El gateway pertenece a una capa separada de `dvk-claude`. El contrato de `dvk-claude` hacia esa capa es la creación/consulta de jobs y la superficie normalizada de `Controller.status`.

El gateway también puede responder localmente si una petición no exige delegación. Para forzar ejecución distribuida, la petición o la propia integración deben indicar que el trabajo debe pasar por el Controller.

## Papel de Desktop Commander

Desktop Commander queda fuera del flujo normal:

```text
Usuario / ChatGPT
   |
   +--> Hermes -> jobs de proyecto
   |
   +--> Desktop Commander -> administración / recuperación
```

Se usa para comprobar servicios y conectividad, reparar Controller/workers, inspeccionar procesos, mantener Windows/WSL o desarrollar la propia infraestructura Hermes cuando esté averiada.

No debe sustituir la cola de Hermes para trabajo normal de proyectos.

## Flujos que no deben confundirse

Incorrecto:

```text
Telegram -> Codex
Telegram -> Claude
Claude <-> Codex
Controller -> SSH interactivo -> worker
Desktop Commander -> Codex para toda tarea
```

Correcto:

```text
interfaz humana
  -> gateway/Director
  -> Controller
  -> worker
  -> engine
  -> Controller
  -> interfaz humana
```

## Pruebas end-to-end verificadas

El sistema ya ha demostrado:

- jobs creados y persistidos por el Controller;
- `main-linux` ONLINE y reclamando jobs;
- ejecuciones reales Codex, Claude, Hybrid y Brainstorm;
- Brainstorm distribuido real con Controller en `hermes01`, worker en `main-linux`, seis llamadas Claude/Codex, cero reintentos, once artefactos verificados y repo sin cambios;
- resultados terminales `DONE` y `FAILED`;
- selección automática `balanced-v1`;
- una tarea enviada desde Telegram que recorrió la cola real hasta `main-linux`;
- exposición normalizada de resultados para integraciones externas.

Una ejecución `FAILED` no implica un fallo de transporte. El job read-only que terminó `error_max_turns` confirmó la cadena Telegram -> Controller -> queue -> main-linux -> Hybrid -> result; el defecto estuvo en la selección/orquestación del motor.

## Próximas mejoras arquitectónicas

1. Distinguir review read-only de review de alto impacto en `balanced-v1`.
2. Implementar `WAIT_USER -> aprobación/rechazo -> resume/cancel`.
3. Conectar la resolución de gates con Telegram.
4. Mantener el gateway como interfaz y el Controller como autoridad de estado.
5. Habilitar `windows-render` sólo cuando su flujo y gates estén probados.
6. Mantener trazabilidad de engine, worker, run, evidencia y resultado en cada delegación.
7. Tras revisión humana, fusionar y desplegar permanentemente Brainstorm v1; hasta entonces el E2E está verificado pero producción sigue en las revisiones anteriores.
