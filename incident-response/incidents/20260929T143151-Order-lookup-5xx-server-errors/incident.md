# Incident

- alertname: Order lookup 5xx server errors
- status: firing
- endpoint: /api/orders/{order_id}
- window: 5m
- severity: warning
- summary: Server errors (5xx) on order lookups
- description: More than 0 order lookup requests failed with a 5xx status in the last 5 minutes. Endpoint: GET /api/orders/{order_id} (window: 5m, evaluation: 20s).
- dashboard: http://localhost:3000/d/order-tracker
- started at: 2026-09-29T14:31:50Z

Files: alert.json, evidence-metrics.json, evidence-logs.json,
evidence-traces.json, agent-response.md (written by the on-call agent).
