# Incident responder

Receives Grafana alerts at `POST /alerts` on port 8001. For every firing alert it:

1. saves the alert and its context to `incidents/<id>/`: request counts (Prometheus), error logs with stack traces (Loki), and failing traces (Tempo), summarized in `context.md`
2. starts Claude Code in headless mode (`claude -p`) in the app directory to investigate, fix, test, and restart the app
3. saves the agent's transcript (`transcript.jsonl`) and final answer (`response.md`)

Repeated notifications for an alert that is still being investigated don't start a second agent. Resolved alerts are ignored.

It runs on the host because it needs your logged-in `claude` CLI and `docker compose`.

```bash
uv run python responder.py
```

Check incidents with `curl localhost:8001/incidents` or `curl localhost:8001/incidents/<id>`. Run tests with `uv run pytest -q`.

| Variable | Default |
| --- | --- |
| `RESPONDER_HOST` / `RESPONDER_PORT` | `127.0.0.1` / `8001` |
| `PROMETHEUS_URL` / `LOKI_URL` / `TEMPO_URL` | `http://localhost:9091` / `:3101` / `:3201` |
| `AGENT_ENABLED` | `true` (`false` only saves context) |
| `AGENT_TIMEOUT` | `1800` seconds |
| `AGENT_ALLOWED_TOOLS` | file tools, plus `uv run`, `docker compose`, `curl`, and read-only `git` |
