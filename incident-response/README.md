# Incident responder

A small service that acts as the automatic on-call engineer for Order Tracker.

## How it works

1. Grafana sends alert webhooks to `POST /alerts` (port 8001).
2. For each alert the responder creates an incident directory under `incidents/` containing:
   - `alert.json` — the full alert payload (labels, annotations, status)
   - `evidence-metrics.json` — current `order_lookup_requests_total` series from Prometheus
   - `evidence-logs.json` — recent app logs from Loki (labels include `trace_id`)
   - `evidence-traces.json` — recent traces from Tempo
   - `incident.md` — human-readable summary (endpoint, window, dashboard link, ...)
3. If the alert is `firing`, the coding assistant is started automatically in
   headless mode (`opencode run`) with the on-call prompt from
   `oncall_prompt.txt`. Its full answer is saved as `agent-response.md`, with
   the last line being `RESOLVED: ...`, `ESCALATE: ...`, or `NO ACTION: ...`.
4. `agent-status.txt` tracks the run (`running` / `done exit=0` / `timeout`).

The webhook is acknowledged immediately (HTTP 202); the agent runs in the
background, so Grafana never times out.

## Run it

```bash
python3 incident-response/server.py
```

No dependencies beyond Python 3. It needs the telemetry stack reachable at
`http://localhost:3000` (Grafana with anonymous admin) to gather evidence.

Endpoints: `POST /alerts`, `GET /incidents` (list incidents and agent status),
`GET /healthz`.

## Configuration (environment variables)

| Variable | Default | Purpose |
| --- | --- | --- |
| `RESPONDER_PORT` | `8001` | Listen port |
| `GRAFANA_URL` | `http://localhost:3000` | Grafana used to fetch evidence |
| `AGENT_CMD` | `opencode run` | Headless coding assistant command (the prompt is appended) |
| `AGENT_TIMEOUT` | `1800` | Agent run timeout in seconds |
| `EVIDENCE_LOOKBACK_MINUTES` | `30` | How far back to fetch logs/traces |

## Grafana webhook (Question 6)

The responder listens on `0.0.0.0`, so Grafana running in Docker reaches it at
`http://host.docker.internal:8001/alerts`.
