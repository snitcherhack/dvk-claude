# DVK Claude — Proyecto Proxy DeepSeek

Este proyecto es un proxy FastAPI que intercepta las llamadas de Claude Code a la API de Anthropic y las redirige a proveedores alternativos. En esta instancia se usa **DeepSeek v4 Flash** a través del provider `deepseek/` con endpoint Anthropic-compatible (no OpenAI chat).

## Configuration

### Variables de entorno (`.env`)
- `ANTHROPIC_AUTH_TOKEN=freecc` — Token local compartido entre proxy y Claude Code
- `DEEPSEEK_API_KEY=<key>` — API key de DeepSeek
- `MODEL=deepseek/deepseek-chat` — Modelo por defecto (DeepSeek v4 Flash / v4 Pro)
- `PORT=8082` — Puerto del proxy

### Cómo arrancar
```bash
# Desde esta carpeta (proxy/)
uv run uvicorn server:app --host 0.0.0.0 --port 8082

# Desde el CLI instalado
free-claude-code
```

### Conectar Claude Code al proxy
```bash
ANTHROPIC_AUTH_TOKEN="freecc" ANTHROPIC_BASE_URL="http://localhost:8082" CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1 claude
```

## Estructura del proyecto
```
proxy/
├── server.py              # ASGI entry point
├── api/                   # FastAPI routes, service layer, routing, optimizations
│   └── models/anthropic.py  # Endpoint /v1/messages
├── providers/
│   └── deepseek/          # Provider DeepSeek (Anthropic Messages transport)
│       ├── client.py      # Cliente HTTP
│       ├── request.py     # Formateo de requests
│       └── response.py    # Parseo de responses
├── core/anthropic/        # Helpers compartidos del protocolo Anthropic
├── config/                # Settings, provider catalog
├── messaging/             # Discord/Telegram bots
├── cli/                   # Entry points
└── tests/
```

## Decisiones técnicas importantes

### Proxy — fixes aplicados
- `api/models/anthropic.py` — Endpoint principal de mensajería, manejo de streaming SSE
- `providers/deepseek/client.py` — Cliente HTTP con soporte Anthropic Messages (no OpenAI chat)
- `providers/deepseek/request.py` — Traducción de formato de requests

### Proveedor DeepSeek
- Usa el endpoint Anthropic-compatible de DeepSeek (`api.deepseek.com/anthropic`), NO el chat completions de OpenAI
- Soportado por el transport `AnthropicMessagesTransport`
- Sin modelo "thinking" activado — se usa DeepSeek v4 Flash para mantener costos bajos

### Logs del servidor
El proxy genera `server.log` (activo) y antes generaba `server.2026-*.log` (logs rotados). Estos logs están en `.gitignore`. NO deben estar presentes en el árbol de trabajo porque inflan el contexto de Claude Code a ~1.4GB. Si aparecen, borrarlos y confirmar que `.gitignore` los excluye.

### CI Checks (orden)
1. `uv run ruff format`
2. `uv run ruff check`
3. `uv run ty check`
4. `uv run pytest`

### Costo y sesiones
- El gasto diario depende del tamaño del contexto acumulado. Hacer `/clear` periódicamente (cuando el contexto supere ~50-60%) reduce drásticamente el costo.
- Este CLAUDE.md existe precisamente para poder hacer `/clear` sin perder el contexto del proyecto.
- `/compact` es una alternativa a `/clear` que comprime el contexto en lugar de borrarlo.

### Sync entre equipos (memoria)
Las memorias de Claude Code se sincronizan entre Windows 10 (casa) y Ubuntu 22 (trabajo) vía GitHub. Ver `~/.claude/projects/-home-jdeiv-free-claude-code/memory/`.
