# Hermes Brainstorm v1 — diseño

Fecha: 2026-09-23

Estado: diseño revisado (Fase 0). Decisiones humanas incorporadas; pendiente de
revisión antes de la Fase 1. Brainstorm **no es operativo** hasta superar el
E2E real descrito en "Despliegue y E2E".

Ámbito: `dvk-claude`

## Objetivo

Añadir a Hermes un workflow de ideación entre Claude y Codex que reciba una
pregunta asociada a un proyecto, genere propuestas independientes, las evalúe
con una rúbrica común y entregue una candidata recomendada para construir un
piloto.

La recomendación representa la mejor hipótesis disponible, no una predicción
de éxito real. Crear código, guiones, assets, renders o publicaciones requiere
un job posterior y una instrucción humana nueva.

Ejemplo de pregunta:

> ¿Cómo podemos idear un nuevo formato de vídeo para el canal de YouTube?

El resultado debe conservar propuestas, puntuaciones, críticas, divergencias,
riesgos y el concepto refinado del ganador en artefactos auditables.

## Decisiones de diseño

- Se implementa como `execution_engine=brainstorm`, siguiendo el precedente de
  `hybrid`: es una orquestación de Hermes, no un modelo.
- Se añade un `BrainstormAdapter` independiente; no se reutiliza la semántica
  implementación/review/fix de `HybridAdapter`.
- Claude y Codex generan ideas sin poder leer la salida del otro. La
  independencia es una propiedad de los roots y del aislamiento de cada etapa,
  no del orden de ejecución (ver "Aislamiento por etapa").
- v1 ejecuta las etapas secuencialmente para mantener simples locks, timeouts
  y recuperación.
- Ambos motores evalúan el mismo conjunto anonimizado.
- Solo Hermes calcula ranking, desempates y confianza, de forma determinista.
  Los evaluadores no devuelven ranking propio.
- El autor de la propuesta ganadora la refina con todas las críticas; el otro
  motor valida el concepto final.
- El workflow es read-only respecto al repositorio. Solo Hermes escribe bajo el
  directorio runtime del job; cada motor escribe únicamente en su directorio de
  etapa.
- El job termina en `DONE` cuando produce un informe válido, con `gate=null`.
  La confirmación para construir el piloto llega como una tarea nueva.
- `brainstorm` puede figurar en `allowed_engines` de un proyecto (opt-in), pero
  está prohibido como `default_engine`. `balanced-v1` no lo selecciona nunca.
  En v1 solo se invoca de forma explícita.
- Un brainstorm nunca se degrada silenciosamente a una sesión de un solo
  modelo.
- La integración del comando conversacional `/brainstorm` en
  Gateway/Telegram queda fuera de este repositorio y se abordará después del
  E2E del Controller y el worker.

## Fuera de alcance

- Automatizar las interfaces gráficas de ChatGPT o Claude.
- Llamar a las API de pago de OpenAI o Anthropic.
- Conversaciones sin límite de rondas.
- Modificar el proyecto durante la ideación.
- Iniciar automáticamente el piloto ganador.
- Forzar un consenso cuando los datos no permiten una recomendación sólida.
- Cambiar la política automática `balanced-v1` o introducir `balanced-v2`.
- Selección automática de Brainstorm.
- Artefactos binarios o base64 inline.
- Convertir `brain`, Drive o un chat en autoridad del estado del job.

## Contrato de tarea

El snapshot mantiene los campos existentes y añade:

```json
{
  "task_type": "brainstorm",
  "execution_engine": "brainstorm",
  "execution_profile": "brainstorm",
  "required_capabilities": ["claude", "codex"],
  "idempotency_policy": "safe_retry",
  "human_gates": [],
  "brainstorm": {
    "version": "brainstorm-v1",
    "candidate_count": 3,
    "rubric_id": "general-v1",
    "rubric": [
      {"id": "value", "label": "Value", "description": "...", "weight": 25},
      "... resto de criterios de general-v1 ..."
    ]
  }
}
```

La rúbrica se **materializa completa** en el task snapshot, también cuando se
usa `general-v1`. Un job encolado no depende de cambios posteriores de la
rúbrica por defecto. `rubric_id` vale `general-v1` (el contenido debe coincidir
exactamente con la rúbrica por defecto) o `custom`.

`required_capabilities` contiene al menos `claude` y `codex`; el builder añade
además las capabilities del proyecto, como hoy.

### Construcción por el task builder

Hoy `build_project_task` toma `task_type` y `execution_profile` del manifest
(`development`/`hermes`). Con `--engine brainstorm` el builder fuerza, ignorando
los valores del manifest:

- `task_type=brainstorm`;
- `execution_profile=brainstorm`;
- `human_gates=[]`: la ideación es read-only y no cruza gates del proyecto;
- `idempotency_policy=safe_retry`: la recuperación se apoya en checkpoints;

y además:

- inyecta el bloque `brainstorm` validado;
- conserva `working_directory`, `runtime_directory`, `allowed_paths` y
  workspaces del proyecto como techo; el adapter reduce después el scope por
  etapa;
- rechaza `--engine brainstorm` si el proyecto no lo declara en
  `allowed_engines`.

CLI:

```bash
python3 -m hermes_controller --runtime-root /path/to/controller \
  task create <project> --engine brainstorm \
  [--candidates 2..6] [--rubric-file rubric.json] \
  --instruction-file question.md
```

`--rubric-file` contiene un **array JSON de criterios**, cada uno con
exactamente `id`, `label`, `description` y `weight`:

```json
[
  {"id": "impact", "label": "Impact", "description": "Expected value.", "weight": 50},
  {"id": "cost", "label": "Cost", "description": "Production cost.", "weight": 30},
  {"id": "risk", "label": "Risk", "description": "Execution risk.", "weight": 20}
]
```

Un array vacío equivale a `general-v1`. `--candidates` y `--rubric-file` solo
se aceptan con `--engine brainstorm`.

La validación del task exige además coherencia: `task_type=brainstorm` o un
bloque `brainstorm` solo son válidos con `execution_engine=brainstorm`.

### Validación del manifest

- `brainstorm` es un valor válido de `allowed_engines`.
- `default_engine=brainstorm` se rechaza.
- `select_engine` (`balanced-v1`) nunca devuelve `brainstorm`; un test lo
  garantiza aunque el proyecto lo permita.

### Parámetros

`candidate_count` admite de 2 a 6 propuestas por motor, es configurable por
task y usa **3** por defecto (6 candidatas totales). Se medirá más adelante si
4 por motor aporta calidad suficiente.

La suma de los pesos de la rúbrica debe ser exactamente 100. Los identificadores
de criterio son únicos, estables y cumplen `[a-z][a-z0-9_]{0,31}`. v1 acepta
entre 3 y 10 criterios.

Si `rubric` está vacía o ausente se aplica `general-v1`:

| id | criterio | peso |
| --- | --- | ---: |
| `value` | Valor esperado para el objetivo | 25 |
| `originality` | Diferenciación y novedad útil | 15 |
| `project_fit` | Encaje con proyecto, audiencia y restricciones | 15 |
| `feasibility` | Viabilidad técnica y operativa | 20 |
| `repeatability` | Potencial para repetirse o escalar | 10 |
| `evidence` | Disponibilidad de datos, fuentes y assets | 10 |
| `pilotability` | Facilidad para validar con un piloto pequeño | 5 |

Cada criterio personalizado contiene `id`, `label`, `description` y `weight`.

El texto de la tarea contiene pregunta, restricciones explícitas y contexto que
deba tener prioridad. Los modelos pueden inspeccionar las rutas autorizadas de
su etapa en modo read-only, pero no pueden ampliar el scope.

## Aislamiento por etapa

### Layout del runtime del job

```text
<run_output_dir>/                   0700, solo Hermes
  hermes-task.md                    snapshot materializado por el worker
  orchestration/                    0700, solo Hermes; nunca visible a un motor
    baseline.json
    budget.json
    checkpoints/<stage>.json
    brainstorm-input.json
    candidates-anonymized.json
    ranking.json
    brainstorm-report.json
    brainstorm-report.md
    brainstorm-report.inline.md     solo si el informe supera 32 KiB
  stages/
    codex-proposals/                task.md + salida del motor
    claude-proposals/
    claude-evaluation/              task.md + input/ preparado por Hermes
    codex-evaluation/
    <author>-refinement/
    <validator>-validation/
```

### Roots por etapa

Cada etapa recibe exactamente:

- `working_directory` en modo read-only;
- su directorio `stages/<stage>/`, que contiene su `task.md`, su `input/`
  (si lo hay) y su salida.

Ninguna etapa recibe el `runtime_directory` del proyecto, el `run_output_dir`
completo, `orchestration/`, el directorio de otra etapa, otros jobs ni los
`allowed_paths`/workspaces extra del proyecto. Los workspaces opt-in del task
pueden añadirse como read-only solo si el task los selecciona explícitamente.

| Etapa | Input preparado por Hermes |
| --- | --- |
| proposals (ambos) | ninguno; solo pregunta, restricciones y rúbrica en `task.md` |
| evaluation (ambos) | `candidates-anonymized.json` + rúbrica |
| refinement | candidata ganadora + críticas de ambos evaluadores sobre ella |
| validation | concepto refinado + pregunta, restricciones y rúbrica |

Las etapas de evaluación nunca reciben las propuestas no anonimizadas.

### Mecanismo por motor

- **Claude**: el hook `PreToolUse` existente deniega cualquier ruta fuera de
  los roots de la etapa; `add_dirs` se limita a esos roots. Herramientas:
  `Read`, `Glob` y `Grep`.
- **Codex**: `--sandbox read-only` limita escrituras, pero **no** lecturas, y
  `--add-dir`/`allowed_paths` tampoco las restringen. El aislamiento de lectura
  de Codex se implementa con un namespace de montaje por etapa cuya vista del
  filesystem es allow-listed: todo lo que no se monte explícitamente no existe
  para el proceso.
  - Verificado en `main-linux`: Codex es un binario Linux nativo
    (`codex-cli 0.154.0`) y los user/mount namespaces sin privilegios
    funcionan (`unshare --user --mount --map-root-user`).
  - Fail-closed: si el aislamiento no está disponible o su autoverificación
    falla, las etapas Codex de Brainstorm terminan `BLOCKED`. No hay fallback
    sin aislamiento.

#### Requisito de aislamiento de Codex

Las herramientas del agente Codex **pueden** leer únicamente:

- el `working_directory` autorizado, en modo read-only;
- el `task.md` y el `input/` de su etapa.

Y debe **verificarse** que **no** pueden leer:

- `orchestration/`;
- los directorios de etapa del otro motor;
- otros jobs ni el runtime del proyecto;
- otras rutas del usuario;
- credenciales, sesiones o ficheros de autenticación de Codex;
- la red.

Montar `~/.codex` completo dentro del namespace **no** se considera aceptable:
`read-only` impide escribir, pero no leer, y expondría la autenticación a las
herramientas del agente. La forma de entregar autenticación al proceso CLI sin
hacerla legible para las herramientas del agente es una incógnita abierta que
resuelve el spike de la Fase 5.

#### Spike de la Fase 5

Se empieza con `unshare`, ya disponible. `bubblewrap` **no** se instala en
este punto. El spike debe comprobar, sin imprimir ni registrar secretos:

1. el sandbox propio de Codex funciona dentro del namespace;
2. aislamiento real de lecturas: una ruta fuera de la allow-list no es legible
   desde las herramientas del agente;
3. aislamiento de credenciales: los ficheros de autenticación y sesiones de
   Codex no son legibles desde las herramientas del agente;
4. red deshabilitada;
5. la autenticación OAuth/suscripción del CLI sigue funcionando;
6. el repositorio es read-only;
7. la salida de la etapa se escribe correctamente.

Las pruebas de legibilidad usan ficheros centinela sin secretos o comprueban
solo existencia/permiso de las rutas de autenticación, nunca su contenido.

Si construir una raíz de filesystem allow-listed segura con `unshare` resulta
compleja o frágil, el trabajo se detiene y se propone `bubblewrap`; su
instalación requiere aprobación humana. Si ningún mecanismo satisface los
siete puntos, las etapas Codex de Brainstorm permanecen `BLOCKED`.

#### Resultado del Spike 5A (2026-09-24, `main-linux`)

Entorno: `codex-cli 0.154.0` nativo
(`~/.codex/packages/standalone/current/bin/codex`), sin instalar paquetes,
layout desechable en `/tmp/hermes-codex-isolation-*` con canaries aleatorios.
Ningún secreto se imprimió, copió ni hasheó; solo se comprobó accesibilidad.

Hechos verificados:

- Codex 0.154 implementa su sandbox Linux con un **`bwrap` incluido en su
  propio paquete** (`codex-resources/bwrap`); sin `bwrap` en `PATH` avisa y usa
  el incluido. No se instaló `bubblewrap` de sistema.
- Con el sandbox `read-only` por defecto, las herramientas del agente **pueden
  leer `~/.codex/auth.json`** y todos los canaries. No sirve para Brainstorm.
- Un **permission profile** de Codex pasado solo por `-c` (sin tocar ficheros
  de `~/.codex`) produce una vista allow-list para las herramientas:

  ```text
  -c 'default_permissions="bs"'
  -c 'permissions.bs.filesystem={":minimal"="read",
       "<home>/.codex/packages"="read", "<repo>"="read", "<stage>"="write"}'
  ```

  Resultado en las herramientas del agente (`codex sandbox` y `codex exec`
  real): repo e input legibles; repo no escribible; stage escribible;
  sibling stage, `orchestration/`, `outside/`, `~/Proyectos`, `~/.claude`,
  `~/.ssh`, `~/.config/dvk-hermes`, `~/.local/state`, `~/.codex/sessions`,
  `/mnt/c` y `/root` **no legibles**; `auth.json` **no legible**. `$HOME` y
  `~/.codex` solo contienen el esqueleto de montaje hasta `packages`. `:minimal`
  expone el runtime del sistema (`/usr`, `/etc`, `/bin`, `/lib*`) respetando
  permisos Unix (`/etc/shadow` no legible).
- La separación de autenticación la da la arquitectura de Codex: el proceso
  principal (fuera del sandbox) lee la sesión de suscripción y llama al
  modelo; las herramientas del agente corren en el sandbox restringido.
- Red de las herramientas: namespace de red propio sin conectividad
  (conexión TCP denegada) creado por el `bwrap` de Codex; no depende solo de
  `network_access=false`.
- Anidamiento: el sandbox de Codex funciona dentro de
  `unshare --user --map-root-user --mount --pid --fork --mount-proc`, tanto con
  `codex sandbox` como con `codex exec` real.
- **Entorno**: en un `codex exec` real el agente vio una variable
  `OUTER_TOKEN_CANARY` heredada; los filtros por defecto de Codex no la
  eliminaron. Lanzando Codex con `env -i HOME=… PATH=… LANG=…` la variable no
  es visible y la autenticación sigue funcionando. El worker real tiene su
  token en el entorno, así que el runner Brainstorm **debe** lanzar Codex con
  entorno limpio.
- Structured output: `STAGE_SCHEMAS["proposals"]` se acepta tal cual con
  `--output-schema`; exit 0, `result.json` presente y payload válido según
  `validate_proposals(..., 2)`. No probados aún con Codex: `evaluation`,
  `refinement`, `validation`.
- `default_permissions` es incompatible con `sandbox_mode`: la ruta Brainstorm
  no puede usar `--profile hermes` ni `--sandbox`.
- Un network namespace externo (`unshare --net`) corta también al proceso
  principal de Codex, que necesita red para el modelo; no es aplicable sin un
  proxy de salida acotado.

Limitaciones abiertas:

- La sintaxis de permission profiles se determinó empíricamente para
  `0.154.0`; debe tratarse como dependiente de versión.
- El proceso principal de Codex persiste sesiones en `~/.codex/sessions`
  (fuera del runtime de Hermes). Resuelto en la Fase 5B con `codex exec
  --ephemeral`: 0 ficheros nuevos en el smoke real.
- No se construyó una raíz allow-listed externa con `pivot_root`: el proceso
  principal debe leer la autenticación y usar la red, así que una vista
  externa no puede ocultarle ninguna de las dos.

Recomendación del spike (pendiente de decisión humana): usar el permission
profile de Codex (con su `bwrap` incluido) como mecanismo de aislamiento de las
herramientas, lanzar Codex con entorno limpio, verificar en cada ejecución con
una sonda de canaries sin modelo (`codex sandbox`) antes de la etapa y fallar
cerrado si algo no coincide; `unshare` user/mount/pid externo como capa
opcional.
- El runner valida que cada ruta recibida esté dentro de los roots de la etapa.

El orden Codex → Claude en proposals se mantiene por simplicidad y como defensa
en profundidad, pero no es la garantía de independencia.

## Componentes

### `BrainstormAdapter`

Nuevo módulo `hermes_controller/brainstorm.py`. Compone dos ejecutores
estructurados: uno Claude y otro Codex. Es responsable de la máquina de
estados, preparación de roots e inputs por etapa, validación semántica,
anonimización, ranking, refinamiento, checkpoints, presupuesto, informe y
artefactos.

El núcleo determinista (rúbrica, validación de etapas, anonimización, ranking,
confianza, sesgo e informe) no hace I/O y se prueba por separado.

### Ejecución estructurada interna

Protocolo interno `StructuredExecutionAdapter`: un orquestador de confianza
solicita una etapa por **nombre de schema**, no por ruta ni por schema
arbitrario. Nombres válidos en v1: `proposals`, `evaluation`, `refinement`,
`validation`. No forma parte del contrato público del Controller.

Los schemas viven en el repositorio (`hermes_controller/schemas/brainstorm/`).
El task del usuario no puede inyectar un schema. `BrainstormAdapter` vuelve a
validar tipos, límites, IDs y referencias después de cada respuesta.

### Claude: perfil `brainstorm`

`ClaudeAgentAdapter` admite el perfil `brainstorm`, read-only (`Read`, `Glob`,
`Grep`) y con texto de modo propio. Se corrige que hoy cualquier perfil
distinto de `claude_smoke` reciba el aviso "Edits are allowed…". Reutiliza
preparación, políticas de ruta, timeouts, SDK, `setting_sources=[]`,
`skills=[]` y structured output, con el schema interno solicitado.

#### API interna implementada (Fase 4)

```python
ClaudeAgentAdapter.execute_structured(
    task, *, stage_schema: str, stage_roots: list[str], timeout_seconds: int,
) -> StructuredExecutionResult          # stage, payload, evidence, metadata
# errores: StructuredExecutionError(status BLOCKED|FAILED, stage, message, evidence)
```

- Argumentos solo por keyword y obligatorios.
- `stage_schema` es un nombre resuelto únicamente desde
  `brainstorm_core.STAGE_SCHEMAS` (copia profunda). Cualquier otro valor,
  incluido un dict, termina `BLOCKED` sin llamar al modelo; los campos del task
  no pueden aportar schema.
- `stage_roots` son exactamente los roots visibles de la etapa, acotados por
  los `authorized_roots` del adapter. `working_directory`, `brain.task_file` y
  `run_output_dir` deben estar dentro de ellos. El hook `PreToolUse` y
  `add_dirs` usan solo esos roots, y en el perfil `brainstorm` el hook deniega
  además cualquier herramienta fuera de `Read`, `Glob`, `Grep`.
- `timeout_seconds` es el presupuesto efectivo de la etapa; el
  `timeout_seconds` del task no se usa en esta ruta.
- El perfil y el `task_type` `brainstorm` deben estar habilitados
  explícitamente en el adapter. `execute()` rechaza el perfil `brainstorm`.
- Opciones SDK del perfil: `tools` y `allowed_tools` = `Read`, `Glob`,
  `Grep`; `disallowed_tools` incluye `Write`, `Edit`, `MultiEdit`, `Bash`,
  `WebFetch`, `WebSearch`, `NotebookEdit`, `Task`, `TodoWrite`;
  `strict_mcp_config=True`, `setting_sources=[]`, `skills=[]`,
  `permission_mode="dontAsk"`. Los perfiles `claude_smoke` y `hermes`
  conservan exactamente sus opciones y prompts anteriores.
- El payload se devuelve sin transformar y sin validación semántica, que
  corresponde a `brainstorm_core` en el `BrainstormAdapter`. Errores del SDK,
  timeout o salida que no sea un objeto JSON se devuelven como
  `StructuredExecutionError`, nunca como resultado Hermes.
- Cada etapa escribe `claude-structured.log` en su directorio con metadatos y
  payload o error.

#### Compatibilidad real de JSON Schema (smoke Fase 4)

Verificado en `main-linux` con `claude-agent-sdk 0.2.156` y Claude Code CLI
`2.1.267`, autenticación por suscripción (sin variables de proveedor externo),
perfil `brainstorm`, repositorio desechable en `/tmp`:

- Los cuatro `STAGE_SCHEMAS` completos se aceptan tal cual y producen payloads
  que además superan la validación semántica de `brainstorm_core`.
- Keywords/estructuras confirmadas: `additionalProperties: false`, objetos
  anidados, arrays anidados (arrays de objetos con arrays dentro), `enum`,
  `integer`, `required` en todos los niveles.
- La restricción `tools=["Read","Glob","Grep"]` no impide la salida
  estructurada.
- El repositorio quedó con huella idéntica y los únicos ficheros nuevos fueron
  los `claude-structured.log` escritos por Hermes en cada directorio de etapa.
- No verificado todavía: `maxLength`, `maxItems`, `minItems` y otros límites
  en el schema; siguen fuera de `STAGE_SCHEMAS` y los límites se aplican en la
  validación semántica.

### Codex: perfil `brainstorm` del runner

`hermes-codex-run.sh` añade:

- `--execution-profile brainstorm`: `--sandbox read-only`,
  `network_access=false`, bootstrap propio (el de `review` habla de revisar
  código y no se reutiliza) y ejecución dentro del namespace de la etapa;
- `--stage-schema proposals|evaluation|refinement|validation`: el runner
  resuelve el fichero bajo su propio directorio. Se rechaza fuera del perfil
  `brainstorm`. No se acepta ninguna ruta de schema externa;
- para ese perfil, la validación posterior comprueba el schema de la etapa en
  lugar del contrato Hermes de seis campos.

`CodexRunAdapter` gana una ruta estructurada separada; su `execute()` actual no
cambia.

#### Implementación (Fase 5B)

Mecanismo aprobado tras el Spike 5A: permission profile nativo de Codex con
su `bwrap` integrado, entorno limpio con `env -i` y sonda sin modelo antes de
cada etapa. No se instala `bubblewrap` de sistema y no se envuelve el proceso
principal de Codex en un network namespace.

- API: `CodexRunAdapter.execute_structured(task, *, stage_schema, stage_roots,
  timeout_seconds) -> StructuredExecutionResult`, idéntica a la de Claude.
  `execute()` rechaza el perfil `brainstorm`; `hermes` y `review` no cambian.
- `run_output_dir` debe ser exactamente uno de los `stage_roots` (el único
  escribible); `working_directory` y `brain.task_file` deben estar dentro.
  `brainstorm_probe.hidden_paths` (opcional, del task construido por Hermes)
  solo endurece la sonda.
- Runner: `hermes-codex-run.sh --execution-profile brainstorm` carga
  `hermes-codex-brainstorm.sh`. Solo acepta `--stage-schema
  proposals|evaluation|refinement|validation`, resuelto a
  `hermes_controller/schemas/brainstorm/<nombre>.schema.json`; cualquier otro
  valor sale con código 2 sin tocar Codex. Rechaza rutas con comillas,
  backslashes o caracteres de control, roots que contengan o estén dentro de
  `CODEX_HOME`, y un binario de Codex fuera de `CODEX_HOME/packages`.
- Schemas (estrategia A): cuatro JSON versionados y un test de igualdad
  canónica con `brainstorm_core.STAGE_SCHEMAS`.
- Permission profile efímero, solo por `-c`:
  `default_permissions="hermes_brainstorm"` y
  `permissions.hermes_brainstorm.filesystem={":minimal"="read",
  "<CODEX_HOME>/packages"="read", <roots de lectura>="read", "<stage>"="write"}`.
  No se modifica `~/.codex/config.toml`.
- Entorno: el adapter lanza el runner con una allow-list positiva (`HOME`,
  `PATH`, `LANG`, `HERMES_CODEX_CLI`, `CODEX_HOME`, `XDG_STATE_HOME` y
  variables de proxy). El runner lanza Codex con `env -i` y solo `HOME`,
  `PATH=/usr/bin:/bin`, `LANG=C.UTF-8`, `CODEX_HOME` si existe y las variables
  de proxy (`HTTPS_PROXY`, `HTTP_PROXY`, `NO_PROXY`, `ALL_PROXY` y minúsculas)
  si existen; un proxy con credenciales (`@`) bloquea la etapa. No se registran
  valores.
- Sonda sin modelo con el mismo array de permisos que `exec`: `READ` y
  `NOWRITE` por root de lectura, `RW` del stage, `NET` (TCP saliente
  denegado), `ENV` (ningún nombre de variable tipo token/secret/key/auth/
  session/cookie/credential), `HIDDEN` para `auth.json`, `sessions`, `~/.ssh`,
  `~/.claude`, `~/.config/dvk-hermes`, `/mnt/c`, un canary privado del runner y
  los `hidden_paths` del task, y `SKELETON`: cada ancestro de una ruta visible
  solo puede mostrar el siguiente componente hacia ella. Si falla, código 3,
  `isolation-probe.json` con `FAIL` y el modelo no se invoca.
- `codex sandbox -C` exige `--permission-profile`, que Codex no permite junto a
  `default_permissions`; la sonda se ejecuta desde el directorio de trabajo sin
  `-C` para usar exactamente el mismo perfil que el modelo.
- Ejecución: `codex exec --ephemeral --ignore-user-config --ignore-rules
  --strict-config -C <wd> -c approval_policy="never" <perfil> --output-schema
  <schema versionado> --output-last-message <directorio privado>`; sin
  `--profile hermes` ni `--sandbox`. El timeout efectivo cubre sonda (máximo
  60 s) y modelo; el adapter añade 30 s de margen solo para matar el runner.
- Resultado y log se escriben primero en un directorio privado del runner y se
  publican en el stage con `mktemp` + `rename`, que reemplaza sin seguir un
  symlink plantado por el agente. El adapter lee con `O_NOFOLLOW`.
  Códigos del runner: 0 ok, 1 fallo de Codex, 2 argumentos/configuración,
  3 sonda fallida, 4 salida no es objeto JSON, 124 presupuesto agotado.

Resultado real en `main-linux` (`codex-cli 0.154.0`, repo desechable en
`/tmp`): las cuatro etapas terminan con la sonda en `PASS` (23 comprobaciones
cada una), payload aceptado con los cuatro schemas versionados tal cual y
validación semántica correcta (`validate_proposals`, `validate_evaluation`,
`validate_refinement`, `validate_validation`, veredicto `PASS`); ningún canary
de sibling, `orchestration/`, `outside/` ni del repo aparece en salida ni log;
repo con huella idéntica; **0 ficheros nuevos en `~/.codex/sessions`** gracias
a `--ephemeral`. Control negativo: el check `ENV` falla con una variable
canary heredada y pasa con `env -i`. El agente ignoró instrucciones del task
que le pedían sondear rutas fuera de su scope, conforme al bootstrap.

Observación: el sandbox de Codex crea en el stage los directorios vacíos
`.agents`, `.codex` y `.git` como puntos de montaje protegidos. Son inocuos;
la Fase 6 debe ignorarlos.

### Artefactos del adapter

`AdapterResult` gana campos internos opcionales `artifacts` y `hashes`.
`result()` conserva exactamente los seis campos públicos actuales. El worker
copia los nuevos campos al envelope externo, que ya los admite. Los adapters
existentes continúan devolviendo listas y mapas vacíos.

## Máquina de estados

1. **Prepare**: valida tarea y rúbrica; comprueba que `run_output_dir` no está
   dentro de `working_directory`; crea el layout; crea o reutiliza
   `baseline.json`.
2. **Codex proposals**: genera N candidatas en su etapa aislada.
3. **Claude proposals**: genera N candidatas en su etapa aislada.
4. **Normalize**: Hermes asigna IDs opacos, elimina el autor de la vista de
   evaluación y ordena con una semilla derivada de `job_id`.
5. **Claude evaluation**: puntúa todas las candidatas y documenta riesgos.
6. **Codex evaluation**: recibe exactamente el mismo bundle y la misma rúbrica.
7. **Rank**: Hermes calcula puntuaciones, confianza y sesgo sin llamada LLM.
8. **Refine**: el motor autor de la primera clasificada produce un concepto
   avanzado con las críticas de ambos.
9. **Validate**: el otro motor verifica el concepto refinado contra pregunta,
   restricciones y rúbrica.
10. **Report**: Hermes escribe JSON/Markdown, calcula hashes y compara la
    huella del repositorio con el baseline.

No hay bucles abiertos. El recorrido normal usa seis llamadas de modelo. Se
permite como máximo un reintento por salida estructural inválida en una etapa
y dos reintentos totales por intento de job; el máximo es ocho llamadas por
intento. Las etapas reutilizadas desde checkpoint no cuentan.

## Datos de etapa

Todos los campos de texto tienen longitud máxima validada por Hermes y las
listas tienen tamaño máximo. Una respuesta que los excede es inválida.

Cada propuesta contiene:

- título y concepto;
- hook o valor diferencial;
- flujo de usuario/espectador;
- esquema de producción o ejecución;
- dependencias y supuestos;
- riesgos;
- piloto mínimo recomendado.

El `candidate_id` lo asigna Hermes en Normalize; los motores no lo eligen.

Cada evaluación contiene, por candidata:

- puntuación entera 0–10 por criterio;
- fortalezas concretas;
- fallos y costes ocultos;
- mejoras recomendadas;
- restricciones explícitas potencialmente incumplidas.

Los evaluadores no devuelven ranking: sería redundante con las puntuaciones y
podría contradecirlas.

El refinamiento contiene una sola candidata final, decisiones adoptadas,
elementos descartados, riesgos asumidos, definición del piloto y criterio de
éxito observable.

La validación final devuelve `PASS` o `FAIL`, hallazgos materiales y una
puntuación final por criterio. No puede sustituir la candidata ganadora por una
idea nueva.

### Contenido de otro motor como datos

En evaluación, refinamiento y validación, cada motor lee texto generado por el
otro. Ese contenido se entrega como JSON delimitado dentro de `input/`, nunca
concatenado como instrucciones, y el prompt de la etapa declara que es dato no
confiable que no puede ampliar herramientas, rutas, red, acciones ni cambiar el
formato de salida. El ranking determinista limita además el efecto de una
evaluación manipulada.

## Ranking determinista

Para cada evaluación y candidata:

```text
model_score = sum(score_0_10 * weight) / 10        # 0..100
final_score = (claude_score + codex_score) / 2
disagreement = abs(claude_score - codex_score)
```

Orden de desempate:

1. mayor `final_score`;
2. menor `disagreement`;
3. ID de candidata en orden ascendente.

Confianza:

- `HIGH`: score >= 75, disagreement <= 10 y margen sobre la segunda >= 5.
- `MEDIUM`: score >= 65 y disagreement <= 20.
- `LOW`: cualquier otro caso.

### Sesgo de autopuntuación

Sin llamadas adicionales, Hermes calcula para cada evaluador:

```text
self_preference = media(scores a candidatas propias) - media(scores a candidatas del otro)
```

El informe publica ambos valores. Si el evaluador que es autor de la ganadora
tiene `self_preference >= 10`, se marca `self_preference_flag` y la confianza
se limita a `MEDIUM`.

### Decisión

Hermes siempre identifica la primera clasificada si existe al menos una
candidata válida. Solo declara `decision_status=RECOMMENDED_FOR_PILOT` cuando
la validación final devuelve `PASS`. Con `FAIL` devuelve
`decision_status=INCONCLUSIVE`, conserva primera y segunda clasificadas y
explica qué debe resolverse.

El ranking no se presenta como consenso. El informe distingue acuerdo,
divergencia, sesgo y cálculo determinista, y se puede reproducir a partir de
los JSON guardados.

### Implementación del núcleo (`hermes_controller/brainstorm_core.py`)

- Código puro: sin filesystem, procesos, red, Controller, worker ni motores.
  El texto de los modelos es dato opaco: se valida tipo y tamaño y se copia;
  ninguna decisión depende de su contenido.
- Aritmética exacta con `fractions.Fraction`. En el informe los números se
  serializan como string exacto: decimal si termina en base 10 (`"74.5"`) y
  `"n/d"` si no (por ejemplo una media de tres valores).
- En las etapas de evaluación y validación, los scores son una lista de
  `{criterion_id, score}` y no un objeto con claves dinámicas, para que el
  schema de etapa sea estático.
- IDs opacos `C01…Cnn` asignados ordenando por
  `SHA-256(canonical_json([job_id, autor, propuesta]))`: reproducible,
  independiente del orden de listas y claves y del `hash()` de Python.
- Con una sola candidata no hay margen sobre la segunda: la confianza máxima
  es `MEDIUM`. Sin candidatas, `LOW` e `INCONCLUSIVE`.
- `self_preference` es `null` para un evaluador sin candidatas propias o sin
  candidatas del otro motor. `self_preference_flag` se activa aunque la
  confianza base ya sea `MEDIUM` o `LOW`; solo baja `HIGH` a `MEDIUM`.
- Divergencias del informe: candidatas con `disagreement > 10` y criterios de
  la ganadora con diferencia >= 3 puntos entre evaluadores.
- `canonical_json`: claves ordenadas, sin espacios, UTF-8, sin NaN.

## Presupuesto de tiempo

`timeout_seconds` del task es el presupuesto **global** del job, no el de cada
llamada. Hermes registra el inicio en `budget.json` y concede a cada etapa
`min(tope_de_etapa, tiempo_restante)`. Si no queda presupuesto para una etapa
obligatoria, el job termina `FAILED` con los artefactos disponibles. El
presupuesto se reinicia en cada intento nuevo del Controller, pero las etapas
ya completadas se reutilizan desde checkpoint.

## Recuperación e idempotencia

El worker vuelve a ejecutar el adapter completo cuando se reinicia con un claim
activo, y el Controller re-encola un job `safe_retry` si la lease caduca. En
ambos casos se reutiliza el mismo `run_output_dir`.

- **Baseline persistente**: `orchestration/baseline.json` se crea en el primer
  intento y se reutiliza siempre después. Nunca se regenera. Si existe pero es
  inválido, el job termina `FAILED`.
- **Checkpoints**: cada etapa terminada guarda
  `checkpoints/<stage>.json` con `schema_version`, `input_sha256` (hash del
  task snapshot, rúbrica e inputs de la etapa) y `output_sha256`. Una etapa se
  reutiliza solo si el checkpoint es válido, coincide `input_sha256` y
  `schema_version`, y el output verifica su hash. En otro caso se recalcula.
- Las etapas deterministas (Normalize, Rank, Report) se recalculan siempre a
  partir de los checkpoints.
- Mientras un intento esté vivo, el worker no inicia otro para el mismo claim;
  el lock del runner de Codex sigue evitando ejecuciones concurrentes.

## Integridad del repositorio

- `run_output_dir` y todos los directorios de etapa deben estar fuera de
  `working_directory`; en otro caso `BLOCKED` antes de llamar modelos.
- El repositorio puede estar dirty, pero su huella inicial se guarda en el
  baseline:
  - `HEAD`;
  - `git status --porcelain=v2 -z --untracked-files=all`;
  - SHA-256 de `git diff --binary HEAD`;
  - SHA-256 de cada fichero no trackeado.
- Todos los comandos Git de Hermes usan `git --no-optional-locks`.
- Si la huella final difiere del baseline, el resultado es `FAILED` y se
  conserva la evidencia de ambas huellas.

## Resultado y artefactos

El resultado público usa:

- `status=DONE` si se creó un informe íntegro, incluso si la decisión es
  `INCONCLUSIVE`;
- `gate=null`;
- `summary` con decisión, candidata y confianza;
- `completed` con las etapas terminadas;
- `remaining` con la confirmación humana o la información necesaria;
- `evidence` con las rutas de los informes y logs.

### Modelo de artefacto

Ampliación mínima y backward-compatible de los elementos de `artifacts`:

```json
{
  "path": "orchestration/brainstorm-report.md",
  "sha256": "...",
  "bytes": 18432,
  "media_type": "text/markdown",
  "inline_text": "...contenido..."
}
```

Un artefacto es **enriquecido** si declara cualquiera de `sha256`, `bytes`,
`media_type` o `inline_text`. Los demás elementos de `artifacts` (por ejemplo
`{"path": "report.json"}`) son **legacy** y no se validan retroactivamente, ni
tampoco sus entradas en `hashes`.

Validación fail-closed de un artefacto enriquecido (`hermes_controller/artifacts.py`):

- solo admite los campos `path`, `sha256`, `bytes`, `media_type`, `inline_text`;
- `path` es relativo a `run_output_dir`, no vacío, sin NUL, sin barra inicial,
  sin letra de unidad, sin backslashes, sin segmentos `..`, `.` ni vacíos;
- `sha256` obligatorio: 64 caracteres hexadecimales en minúscula;
- `bytes` obligatorio: entero >= 0;
- `media_type` opcional: `tipo/subtipo`;
- `inline_text` opcional: string UTF-8 codificable de como máximo 32768 bytes;
- con `inline_text`, `bytes == len(inline_text.encode("utf-8"))` y
  `sha256 == SHA-256(inline_text)`: el texto inline representa exactamente el
  artefacto y nunca es una copia truncada;
- `hashes[path]` debe existir y ser igual a `sha256`;
- no puede haber dos artefactos enriquecidos con el mismo `path`.

Reglas de transporte:

- Sin binarios ni base64 inline en v1. En Brainstorm v1 solo lleva
  `inline_text` el informe principal.
- Si `brainstorm-report.md` supera 32 KiB, Hermes genera
  `brainstorm-report.inline.md`, una versión resumida <= 32 KiB con su propio
  path y hash. Esa versión se transporta inline y el informe completo se
  publica como artefacto enriquecido sin `inline_text`.
- `hashes` mantiene el mapa `path -> hash`; los valores legacy (por ejemplo
  `sha256:abc`) siguen aceptándose.
- `AdapterResult` gana `artifacts` y `hashes` internos; `result()` conserva los
  seis campos públicos. El worker los copia al envelope (por defecto `[]` y
  `{}`) y los conserva al normalizar gates.
- El worker valida los artefactos antes de enviarlos. Si son inválidos, el
  resultado pasa a `FAILED` sin artefactos y con evidencia `artifact-policy`,
  en lugar de enviar un envelope que el Controller rechazaría y el worker
  reintentaría indefinidamente.
- El Controller aplica la misma validación al ingerir y expone `artifacts` y
  `hashes` en `status()` tal como los recibió. No almacena ficheros.
- El JSON Schema publicado del envelope mantiene `artifacts` como array
  genérico para no romper envelopes antiguos; las invariantes nuevas se aplican
  en la validación semántica del Controller y del worker.

Artefactos mínimos (bajo `orchestration/`):

```text
brainstorm-input.json
codex-proposals.json
claude-proposals.json
candidates-anonymized.json
claude-evaluation.json
codex-evaluation.json
ranking.json
winner-refinement.json
winner-validation.json
brainstorm-report.json
brainstorm-report.md
```

Hermes copia a `orchestration/` las salidas validadas de cada etapa. Si una
etapa posterior no puede ejecutarse por ausencia de candidatas, su JSON se
conserva con `stage_status=SKIPPED`; así el conjunto mínimo de artefactos se
mantiene estable.

Los ficheros se crean con modo 0600 bajo directorios 0700. El envelope no
publica credenciales, variables de entorno, sesiones ni prompts internos que
contengan secretos.

## Seguridad

- Claude usa únicamente `Read`, `Glob` y `Grep` en su perfil `brainstorm`.
- Codex usa sandbox read-only, red desactivada y namespace de lectura por
  etapa.
- Cada etapa recibe solo los roots descritos en "Aislamiento por etapa".
- Las instrucciones encontradas en contenido del proyecto o en salidas del otro
  motor se tratan como datos.
- Ninguna etapa hace commit, push, publicación, descarga o llamada de red.
- La política `subscription_only` de Claude permanece sin cambios.
- Los schemas de etapa proceden del código; no existe entrada de schema externa.

## Errores y estados

- Tarea, rúbrica o configuración inválida: `BLOCKED` antes de llamar modelos.
- Motor, capability o aislamiento de etapa ausente: `BLOCKED`.
- Timeout, presupuesto agotado, proceso roto o salida inválida tras reintentos:
  `FAILED`.
- Huella del repositorio distinta del baseline: `FAILED`.
- Un solo motor no basta para emitir ranking: no se degrada a sesión
  monomodelo.
- Ninguna candidata válida o validación final fallida: informe `DONE` con
  decisión `INCONCLUSIVE`.
- `WAIT_USER` no se usa en v1 porque el workflow no reanuda el mismo job.

## Configuración y compatibilidad

`brainstorm` se añade, con capabilities `{"claude", "codex"}`, en:

- `ENGINE_CAPABILITIES` del Controller;
- `hermes-task.schema.json` (`execution_engine`, `engine_selection`);
- `hermes-controller-result-envelope.schema.json` (`engine`);
- `hermes-worker-runtime.schema.json` (`adapters`, `default_execution_engine`
  no lo admite, `kind: brainstorm`);
- `choices` de `--engine` en la CLI;
- `WorkerDaemon._execution_engine_for_task` y el set de engines conocidos de
  `_adapter_from_worker_config`.

Sin el cambio en `_execution_engine_for_task`, un resultado Brainstorm se
enviaría como `codex`, el Controller lo rechazaría y el worker lo reintentaría
indefinidamente, porque hoy solo descarta el envelope pendiente ante un 409.
Ese comportamiento ante rechazos no-409 es un defecto preexistente, fuera del
alcance de v1, pero queda registrado.

`WorkerDaemon` construye `BrainstormAdapter` después de los adapters Claude y
Codex que referencia, como ya hace con `hybrid`. La configuración versionada de
`main-linux` pasa del adapter único actual a `adapters`, anuncia ambas
capabilities y registra Brainstorm. No se guardan rutas secretas ni tokens en
Git; se mantienen referencias mediante variables de entorno.

Antes de declarar el modo operativo debe comprobarse la configuración realmente
desplegada, porque la documentación registra E2E anteriores de Claude/Hybrid
pero el `main-linux.json` versionado actual solo configura Codex.

Los motores existentes, tasks sin `execution_engine`, resultados heredados,
artefactos existentes y `HybridAdapter` conservan su comportamiento.
`balanced-v1` no cambia.

## Pruebas

Pruebas unitarias:

- validación de manifest (opt-in, prohibido como default) y de `balanced-v1`
  sin Brainstorm;
- builder: perfil, tipo, bloque, `candidate_count` por defecto y límites;
- validación de rúbrica y de schemas de etapa, incluidos límites de longitud;
- independencia: el `task.md` y los roots de proposals no contienen datos ni
  rutas del otro motor; evaluación sin propuestas no anonimizadas;
- anonimización y orden estable;
- score, confianza, desempates y sesgo de autopuntuación;
- selección del autor para refine y del oponente para validate;
- límites de llamadas, reintentos y presupuesto global;
- baseline persistente, checkpoints reutilizados e invalidados;
- huella del repositorio y `run_output_dir` dentro del working tree;
- semántica `PASS`, `FAIL`, `BLOCKED` y `FAILED`;
- artefactos: permisos, hashes, `inline_text` <= 32 KiB y versión resumida;
- compatibilidad de `AdapterResult`, envelopes y artefactos anteriores.

Pruebas de integración:

- worker multi-engine construye Codex, Claude y Brainstorm;
- runner Codex acepta `--stage-schema` solo en perfil `brainstorm`;
- perfil Codex read-only, sin red y aislado: un fichero fuera de los roots de
  la etapa no es legible;
- perfil Claude sin Write/Edit/Bash/red y con el hook limitado a la etapa;
- Controller encola, reclama, ingiere y expone un resultado Brainstorm con
  artefacto inline;
- retry de ingestión conserva idempotencia del envelope.

Validación final:

```bash
python3 -m pytest tests -q
python3 -m compileall -q hermes_controller
git diff --check
```

## Despliegue y E2E

El E2E real requiere desplegar esta rama en el Controller de `hermes01` y en
`main-linux` antes del merge, porque cambian Controller, builder y validación.

**No se despliega el Controller de esta rama sin el worker compatible** (Fase 7).
Con solo el Controller nuevo, un worker desplegado que anuncie `claude` y
`codex` reclamaría un job Brainstorm, respondería `BLOCKED` por engine no
configurado y enviaría el envelope como `codex`; el Controller lo rechazaría
por engine distinto y el worker lo reintentaría indefinidamente.
Ese despliegue, y la instalación de `bubblewrap` si llegara a proponerse,
requieren aprobación humana explícita.

Secuencia:

1. Smoke local en `main-linux` con repositorio desechable: cada motor por
   separado y después el flujo completo, sin red.
2. E2E distribuido:
   `Controller hermes01 -> queue -> main-linux -> BrainstormAdapter ->
   Claude/Codex -> ranking/refine/validate -> ingest -> DONE`, con pregunta
   inocua, ambos motores autenticados por suscripción, artefactos y hashes
   publicados, informe inline visible en `Controller.status` y repositorio con
   huella idéntica.

Solo entonces se documenta Brainstorm como operativo.

### Documentación canónica antes del merge

Antes del merge definitivo deben actualizarse:

- `docs/hermes-communication-architecture.md`
- `docs/hermes-project-registry.md`
- `docs/hermes-claude-integration.md`

Hasta superar el E2E distribuido, esos documentos describen Brainstorm como
implementado/experimental o pendiente de E2E, nunca como operativo.

## Criterios de aceptación

- Una tarea explícita Brainstorm recorre las diez etapas acotadas.
- Ninguna etapa de proposals puede leer el runtime del job, `orchestration/` ni
  la etapa del otro motor, con independencia del orden.
- Ranking, confianza y sesgo se reproducen a partir de los JSON guardados.
- El informe identifica una candidata para piloto o explica por qué la decisión
  es inconclusa, y llega inline (<= 32 KiB) al Controller.
- Un reintento reutiliza baseline y checkpoints sin repetir llamadas completas.
- No se modifica el repositorio ni se cruza ningún gate.
- Controller conserva autoridad sobre job, run, lease, resultado y artefactos.
- Las rutas, perfiles, schemas y capabilities se validan fail-closed.
- `balanced-v1` no selecciona Brainstorm.
- Codex, Claude, Hybrid y tasks heredadas mantienen su suite verde.

## Relación con el experimento `0a42c0d`

La rama experimental `feat/brainstorm-balanced-v2` de `hermes01` no se adopta.
Se reutilizan solo sus ideas: perfil Claude read-only, configuración
multi-engine de `main-linux`, tests de independencia de prompts y tests de
routing. `collaboration_mode` no es necesario porque Brainstorm es un engine
propio; `balanced-v2` queda aplazado.

## Orden de implementación

0. Este documento.
1. Contrato y Controller: engine, enums, validación de manifest, builder y CLI.
2. `AdapterResult.artifacts/hashes`, modelo de artefacto e `inline_text`.
3. Núcleo determinista sin I/O.
4. Claude: perfil `brainstorm` y ejecución estructurada por etapa.
5. Runner Codex: perfil `brainstorm`, `--stage-schema` y aislamiento por
   namespace, empezando por el spike `unshare` y sus siete comprobaciones.
6. `BrainstormAdapter`: layout, roots, baseline, huella, checkpoints,
   presupuesto y estados.
7. Worker: `kind: brainstorm` y `main-linux.json` multi-engine.
8. Integración local Controller + worker con motores falsos.
9. Smoke real en `main-linux`.
10. E2E distribuido con aprobación de despliegue.
11. Actualización de los tres documentos canónicos (experimental hasta el E2E)
    antes del merge.
