@AGENTS.md

# Instrucciones específicas de Claude Code

## Alcance

Este archivo se aplica a sesiones interactivas de Claude Code (terminal, app de
escritorio y `codex-plugin-cc`). El adapter `claude-agent` de Hermes usa
`setting_sources=[]` y `skills=[]`, por lo que no carga este archivo. El
comportamiento de un job debe proceder de su task snapshot, su execution
profile y el adapter.

## Entorno

- Trabaja en el clon WSL `/home/deiv/Proyectos/dvk-claude` y ejecuta comandos
  en Linux. Usa `\\wsl.localhost\Ubuntu-24.04\...` solo cuando una interfaz de
  Windows necesite abrir la carpeta.
- Claude Code en WSL usa el binario nativo autenticado mediante `claude.ai`.
  No introduzcas `ANTHROPIC_API_KEY` ni selectores Bedrock, Vertex o Foundry:
  el worker usa `subscription_only=true` y los rechaza.

## Coordinación con Codex

- En jobs autónomos, Claude y Codex se coordinan únicamente mediante una
  máquina de estados de Hermes, como Hybrid.
- En sesiones humanas, `codex-plugin-cc` puede utilizarse para review, rescue o
  transfer. El daemon no debe depender del plugin ni de comandos interactivos.

## Cambios en adapters o policy

- Conserva `claude-agent` fail-closed. Los hooks de rutas,
  `authorized_roots`, la normalización UNC de WSL y la versión mínima del SDK
  son límites de seguridad.
- Cualquier cambio en `balanced-v1` debe actualizar sus pruebas y la
  descripción correspondiente en `docs/hermes-project-registry.md` y
  `docs/hermes-communication-architecture.md`.
