# AGENTS.md — dvk-claude (Hermes)

Instrucciones compartidas para cualquier agente de programación que trabaje en
este repositorio, incluidos Codex y Claude Code. Mantén este archivo breve: la
arquitectura canónica vive en `docs/`.

## Qué contiene este repositorio

`dvk-claude` contiene la capa actual de orquestación multiagente de Hermes:
`hermes_controller` (Controller, registry, policy, queue, adapters y transporte),
configuración de workers, runners de motores y lanzadores de Claude/Codex.

En este host, el checkout autoritativo es el clon WSL:
`/home/deiv/Proyectos/dvk-claude`. Otros clones, incluido el de Windows, pueden
estar atrasados o contener trabajo local ajeno. No los sincronices, resetees,
guardes en stash ni sobrescribas sin una instrucción explícita.

## Lectura inicial

Antes de analizar o cambiar la arquitectura, lee:

1. `docs/hermes-communication-architecture.md`: componentes, comunicación y
   autoridad del estado.
2. `docs/hermes-project-registry.md`: manifests, task builder, resultados y
   gates.
3. `docs/hermes-claude-integration.md`: motores, adapters y flujos
   Codex/Claude/Hybrid.

Referencia estos documentos; no dupliques su contenido en otros repositorios.

## Arquitectura resumida

Interfaz humana (Telegram u otra) -> Gateway/Director -> Controller -> worker ->
motor (Codex, Claude, Hybrid o native) -> Controller -> interfaz. El Controller
es la autoridad de proyectos, workers, jobs, runs, leases, eventos, resultados
y gates. Los workers hacen polling/claim; su filesystem no es el estado global.
Codex y Claude no se comunican directamente: Hybrid es una máquina de estados
controlada por Hermes.

## Mapa del repositorio

- `hermes_controller/`: Controller, registry, policy, adapters y transporte.
- `config/hermes-projects/`: manifests. Su presencia no prueba que el proyecto
  esté registrado o activo; comprueba `project list` en el Controller.
- `config/hermes-workers/`: configuración versionada de workers. Verifica el
  runtime antes de afirmar qué configuración está desplegada.
- `hermes-codex-run.sh`: runner Codex genérico para trabajo nuevo.
- `cxh-run.sh`: compatibilidad heredada de Winner Timeline.
- `hermes-run-result.schema.json`: contrato de resultado del runner.
- `tests/`: suite principal.
- `legacy/`: material histórico, no comportamiento operativo actual.

## Orden de autoridad

Las instrucciones humanas explícitas de la sesión prevalecen. Para determinar
el estado operativo usa, por este orden:

1. Git real y evidencia runtime verificada.
2. `brain/proyectos/<proyecto>/ESTADO.md`.
3. `brain/proyectos/<proyecto>/TAREA_ACTIVA.md`.
4. Los tres documentos canónicos de `docs/` indicados arriba.
5. `MEMORY`, checkpoints y documentos legacy, solo como historia.

No conviertas documentos antiguos en verdad operativa actual.

## Gates y seguridad

- Deivid es la autoridad humana final. Nunca infieras su aprobación.
- `WAIT_USER` es fail-closed y solo es válido con un gate declarado por la
  tarea o el proyecto.
- La resolución/reanudación de gates mediante Telegram sigue pendiente; no la
  declares operativa sin un E2E verificado.
- No expongas ni persistas tokens, claves API, credenciales o contenidos de
  `.env`.
- No publiques, despliegues infraestructura, ejecutes pentesting activo,
  realices migraciones destructivas ni cruces un gate sin aprobación explícita.
- Conserva cambios locales ajenos. Inspecciona rama, HEAD, remoto y working tree
  antes de modificar un repositorio.
- No uses `git reset --hard`, `git clean` indiscriminado ni stash ciego.

## Reglas operativas

- Distingue siempre entre estado verificado, inferencia, propuesta y acción
  pendiente.
- No inventes rutas, versiones, resultados, evidencia ni integraciones.
- Los jobs de producción pasan por Controller -> worker -> adapter. Desktop
  Commander es un canal de administración, diagnóstico y recuperación.
- Cada worker de desarrollo usa un clon o worktree independiente. En Linux/WSL
  prefiere checkouts nativos bajo `/home/deiv/Proyectos`.
- Una integración solo es operativa cuando dispone de un E2E verificado.
- Una tarea no está completa sin evidencia verificable.

## Pruebas

Desde la raíz del repositorio ejecuta:

```bash
python3 -m pytest tests -q
```

No ejecutes el `pytest` raíz sin limitarlo a `tests/`: también recogería
`proxy/`, que tiene dependencias propias. Informa del recuento exacto de éxitos
y fallos.

## Cambios propuestos

Describe qué existe, qué limitación está verificada, qué propones y qué exige
aprobación humana. Identifica la capa afectada: gateway, Controller,
registry/policy, queue/job, worker, adapter/engine, gate o proyecto.

Prefiere cambios pequeños, comprobables y reversibles. Conserva el
comportamiento fail-closed y la compatibilidad de los contratos actuales,
incluidos `execution_engine` opcional y la aceptación temporal de
`codex_result` heredado.

## Repositorios relacionados

La documentación canónica confirma `brain` (estado y tareas),
`video-automation` (generación/render) y `video-publisher` (metadata y futura
publicación sujeta a gates). `hermes-informes` conserva trazabilidad, pero no es
un componente runtime. No trates otros repositorios como parte operativa de
Hermes sin verificar registry y estado real.

Prodigy, `lideryoutube` y la cadena DeepSeek pertenecen al flujo histórico de
YouTube; no los presentes como arquitectura actual de Hermes.
