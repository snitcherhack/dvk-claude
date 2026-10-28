# Hermes Gates Plugin

Deterministic human-gate bridge for Hermes Agent messaging surfaces.

Commands:

- `/gates` — list current `WAIT_USER` gates from the Hermes Controller.
- `/gate approve <job-prefix> [note]`
- `/gate reject <job-prefix> [note]`

The plugin always refreshes `/v1/gates/pending` immediately before resolving a gate and posts the exact `source_run_id`. A stale or conflicting decision is therefore rejected by the Controller with HTTP 409.

Runtime environment:

- `HERMES_OPERATOR_TOKEN` — required secret.
- `HERMES_CONTROLLER_URL` — optional; defaults to `http://127.0.0.1:8787`.
- `HERMES_GATE_ACTOR` — optional audit actor; defaults to `telegram-operator`.

No token values belong in Git.

## Production deployment contract

Install the plugin under `~/.hermes/plugins/hermes-gates/` and enable it with
`hermes plugins enable hermes-gates`. Do not grant built-in tool override
permission; this plugin registers only slash commands.

The Gateway must receive only the operator credential needed by this bridge.
Use a dedicated mode-600 environment file for `HERMES_OPERATOR_TOKEN` (plus
the optional non-secret URL/actor variables). Do not attach the Controller's
full environment file to the Gateway because it also contains worker bootstrap
credentials that the bridge does not need.

Restart the Gateway after enabling the plugin or changing its environment.
