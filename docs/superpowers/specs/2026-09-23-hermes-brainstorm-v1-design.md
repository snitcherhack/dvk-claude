# Hermes Brainstorm v1 — diseño

Fecha: 2026-09-23

Estado: diseño aprobado en conversación; pendiente de revisión del documento

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
- Claude y Codex generan ideas sin ver la salida del otro.
- La independencia es lógica. v1 ejecuta etapas secuencialmente para mantener
  simples los locks, timeouts y la recuperación.
- Ambos motores evalúan el mismo conjunto anonimizado.
- Hermes calcula ranking, desempates y confianza de forma determinista.
- El autor de la propuesta ganadora la refina con todas las críticas; el otro
  motor valida el concepto final.
- El workflow es read-only respecto al repositorio. Solo Hermes escribe bajo el
  directorio runtime del job.
- El job termina en `DONE` cuando produce un informe válido, con `gate=null`.
  La confirmación para construir el piloto llega como una tarea nueva.
- `balanced-v1` no selecciona Brainstorm automáticamente en v1. El motor se
  solicita de forma explícita.
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
- Cambiar la política automática `balanced-v1`.
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
    "candidate_count": 4,
    "rubric": []
  }
}
```

`candidate_count` admite de 2 a 6 propuestas por motor y usa 4 por defecto.
La suma de los pesos de la rúbrica debe ser exactamente 100. Los identificadores
de criterio son únicos y estables.

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

Cada criterio personalizado contiene `id`, `label`, `description` y
`weight`. v1 acepta entre 3 y 10 criterios.

El texto de la tarea contiene pregunta, restricciones explícitas y contexto que
deba tener prioridad. Los modelos pueden inspeccionar rutas autorizadas en modo
read-only, pero no pueden ampliar el scope del task.

## Componentes

### `BrainstormAdapter`

Nuevo módulo `hermes_controller/brainstorm.py`. Compone dos ejecutores
estructurados: uno Claude y otro Codex. Es responsable de la máquina de estados,
validación semántica, anonimización, ranking, refinamiento, informe y artefactos.

### Ejecución estructurada interna

Se añade un protocolo interno `StructuredExecutionAdapter` que permite a un
orquestador de confianza solicitar un schema JSON específico. No forma parte
del contrato público del Controller.

`ClaudeAgentAdapter` reutiliza su preparación de task, políticas de ruta,
timeouts, SDK y structured output, pero admite el schema interno suministrado
por `BrainstormAdapter`.

`CodexRunAdapter` escribe el schema de etapa dentro del `run_output_dir` y
llama a `hermes-codex-run.sh` con `--result-schema-file`. El runner acepta
este flag únicamente con `execution_profile=brainstorm`, exige que la ruta
esté dentro del runtime autorizado y mantiene `--sandbox read-only` y red
desactivada.

Los schemas internos proceden del código de Hermes; el task del usuario no
puede inyectar un schema arbitrario. `BrainstormAdapter` vuelve a validar
tipos, límites, IDs y referencias después de cada respuesta.

### Artefactos del adapter

`AdapterResult` gana campos internos opcionales `artifacts` y `hashes`.
`result()` conserva exactamente los seis campos públicos actuales. El worker
copia los nuevos campos al envelope externo, que ya admite ambos. Los adapters
existentes continúan devolviendo listas y mapas vacíos.

## Máquina de estados

1. **Prepare**: valida tarea y rúbrica, crea runtime privado y registra HEAD y
   `git status --porcelain` iniciales.
2. **Claude proposals**: genera N candidatas sin datos de Codex.
3. **Codex proposals**: genera N candidatas sin datos de Claude.
4. **Normalize**: Hermes asigna IDs opacos, elimina el nombre del autor de la
   vista de evaluación y ordena con una semilla derivada de `job_id`.
5. **Claude evaluation**: puntúa todas las candidatas y documenta riesgos.
6. **Codex evaluation**: recibe exactamente el mismo bundle y la misma rúbrica.
7. **Rank**: Hermes calcula puntuaciones y confianza sin una llamada LLM.
8. **Refine**: el motor autor de la primera clasificada produce un concepto
   avanzado incorporando las críticas de ambos.
9. **Validate**: el otro motor verifica el concepto refinado contra pregunta,
   restricciones y rúbrica.
10. **Report**: Hermes escribe JSON/Markdown, calcula hashes y comprueba que HEAD
    y working tree no han cambiado.

No hay bucles abiertos. El recorrido normal usa seis llamadas de modelo. Se
permite como máximo un reintento por salida estructural inválida y dos
reintentos totales por job; por tanto el máximo es ocho llamadas.

## Datos de etapa

Cada propuesta contiene:

- `candidate_id` asignado por Hermes;
- título y concepto;
- hook o valor diferencial;
- flujo de usuario/espectador;
- esquema de producción o ejecución;
- dependencias y supuestos;
- riesgos;
- piloto mínimo recomendado.

Cada evaluación contiene, por candidata:

- puntuación entera 0–10 por criterio;
- fortalezas concretas;
- fallos y costes ocultos;
- mejoras recomendadas;
- restricciones explícitas potencialmente incumplidas;
- ranking completo sin empates.

El refinamiento contiene una sola candidata final, decisiones adoptadas,
elementos descartados, riesgos asumidos, definición del piloto y criterio de
éxito observable.

La validación final devuelve `PASS` o `FAIL`, hallazgos materiales y una
puntuación final por criterio. No puede sustituir la candidata ganadora por una
idea nueva.

## Ranking determinista

Para cada evaluación y candidata:

```text
model_score = sum(score_0_10 * weight) / 10
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

Hermes siempre identifica la primera clasificada si existe al menos una
candidata válida. Solo declara `decision_status=RECOMMENDED_FOR_PILOT` cuando
la validación final devuelve `PASS`. Con `FAIL` devuelve
`decision_status=INCONCLUSIVE`, conserva primera y segunda clasificadas y
explica qué debe resolverse.

El ranking no se presenta como consenso. El informe distingue acuerdo,
divergencia y cálculo determinista.

## Resultado y artefactos

El resultado público usa:

- `status=DONE` si se creó un informe íntegro, incluso si la decisión es
  `INCONCLUSIVE`;
- `gate=null`;
- `summary` con decisión, candidata y confianza;
- `completed` con las etapas terminadas;
- `remaining` con la confirmación humana o información necesaria;
- `evidence` con las rutas de los informes y logs.

Artefactos mínimos:

```text
brainstorm-input.json
claude-proposals.json
codex-proposals.json
candidates-anonymized.json
claude-evaluation.json
codex-evaluation.json
ranking.json
winner-refinement.json
winner-validation.json
brainstorm-report.json
brainstorm-report.md
```

Si una etapa posterior no puede ejecutarse por ausencia de candidatas, su JSON
se conserva con `stage_status=SKIPPED`; así el conjunto mínimo de artefactos se
mantiene estable.

Los ficheros se crean con modo 0600 bajo un directorio 0700. El envelope
publica rutas relativas al runtime y hashes SHA-256; no publica credenciales ni
prompts internos que contengan secretos.

## Seguridad y aislamiento

- Claude usa únicamente `Read`, `Glob` y `Grep`.
- Codex usa sandbox read-only y red desactivada.
- Ambos reciben solo `working_directory`, task snapshot y `allowed_paths`.
- Las instrucciones encontradas en contenido del proyecto se tratan como datos;
  no pueden ampliar herramientas, rutas, red ni acciones.
- El adapter bloquea rutas fuera de los roots autorizados.
- El repositorio puede estar dirty, pero su estado inicial se registra. Si HEAD
  o `git status --porcelain` cambia durante el job, el resultado es `FAILED`
  y conserva evidencia.
- Ninguna etapa hace commit, push, publicación, descarga o llamada de red.
- La política `subscription_only` de Claude permanece sin cambios.
- Logs y artefactos no contienen tokens, variables de entorno ni sesiones.

## Errores y estados

- Tarea, rúbrica o configuración inválida: `BLOCKED` antes de llamar modelos.
- Motor/capability ausente: `BLOCKED`.
- Timeout, proceso roto o salida inválida tras reintentos: `FAILED`.
- Un solo motor no basta para emitir ranking: no se degrada silenciosamente a
  una sesión monomodelo.
- Ninguna candidata válida o validación final fallida: informe `DONE` con
  decisión `INCONCLUSIVE`.
- `WAIT_USER` no se usa en v1 porque el workflow no reanuda el mismo job.

## Configuración y compatibilidad

Se amplían los enums de task, project, worker y envelope para incluir
`brainstorm`. Su capability requerida es `{"claude", "codex"}`.

`EngineRoutingAdapter` construye `BrainstormAdapter` después de crear los
adapters Claude y Codex que este referencia. La configuración versionada de
`main-linux` debe pasar del adapter único actual a `adapters`, anunciar ambas
capabilities y registrar Brainstorm. No se guardan rutas secretas ni tokens en
Git; se mantienen referencias mediante variables de entorno.

Antes de declarar el modo operativo debe comprobarse la configuración realmente
desplegada, porque la documentación registra E2E anteriores de Claude/Hybrid
pero el `main-linux.json` versionado actual solo configura Codex.

Los motores existentes, tasks sin `execution_engine`, resultados heredados y
`HybridAdapter` conservan su comportamiento. `balanced-v1` no cambia.

## Pruebas

Pruebas unitarias:

- validación de configuración y rúbrica;
- independencia de prompts de propuesta;
- anonimización y orden estable;
- validación de schemas de propuestas/evaluaciones;
- cálculo de score, confianza y desempates;
- selección del autor para refine y del oponente para validate;
- límites de llamadas y reintentos;
- semántica `PASS`, `FAIL`, `BLOCKED` y `FAILED`;
- detección de modificación del repositorio;
- artefactos, permisos y hashes;
- compatibilidad de `AdapterResult` y envelopes anteriores.

Pruebas de integración:

- worker multi-engine construye Codex, Claude y Brainstorm;
- runner Codex acepta el schema interno solo en perfil Brainstorm;
- perfil Codex es read-only y sin red;
- perfil Claude no expone Write/Edit/Bash/red;
- Controller encola, reclama, ingiere y expone un resultado Brainstorm;
- retry de ingestión conserva idempotencia del envelope.

Validación final:

```bash
python3 -m pytest tests -q
python3 -m compileall -q hermes_controller
git diff --check
```

El E2E real usa un repositorio desechable o un checkout dedicado, una pregunta
inocua, ambos motores autenticados por suscripción y ningún acceso de red. Debe
terminar `DONE`, publicar artefactos y hashes, y dejar el repositorio idéntico.
Solo entonces se documenta Brainstorm como operativo.

## Criterios de aceptación

- Una tarea explícita Brainstorm recorre las diez etapas acotadas.
- Las propuestas iniciales no incluyen salida del otro motor.
- Ranking y confianza se reproducen a partir de los JSON guardados.
- El informe identifica una candidata para piloto o explica por qué la decisión
  es inconclusa.
- No se modifica el repositorio ni se cruza ningún gate.
- Controller conserva autoridad sobre job, run, lease, resultado y artefactos.
- Las rutas, perfiles y capabilities se validan fail-closed.
- Codex, Claude, Hybrid y tasks heredadas mantienen su suite verde.
