"""Incident responder for Order Tracker.

Receives Grafana alert webhooks at POST /alerts on port 8001. For each alert
it saves the payload plus telemetry evidence (metrics from Prometheus, logs
from Loki, traces from Tempo, all via the Grafana APIs) in an incident
directory, then starts the coding assistant in headless mode as the on-call
engineer. The webhooks are acknowledged immediately; the agent runs in the
background and its answer is saved next to the evidence.

Run with:  python3 incident-response/server.py
"""

import json
import os
import re
import shlex
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = int(os.getenv("RESPONDER_PORT", "8001"))
GRAFANA_URL = os.getenv("GRAFANA_URL", "http://localhost:3000")
AGENT_CMD = os.getenv("AGENT_CMD", "opencode run")
AGENT_TIMEOUT = int(os.getenv("AGENT_TIMEOUT", "1800"))
LOOKBACK_MINUTES = int(os.getenv("EVIDENCE_LOOKBACK_MINUTES", "30"))

BASE_DIR = Path(__file__).resolve().parent
INCIDENTS_DIR = BASE_DIR / "incidents"
REPO_DIR = BASE_DIR.parent

PROMPT_TEMPLATE = (BASE_DIR / "oncall_prompt.txt").read_text()

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def grafana_get(path, params):
    url = f"{GRAFANA_URL}{path}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=15) as response:
            return json.load(response)
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def gather_evidence(incident_dir, now):
    start_s = int(now.timestamp()) - LOOKBACK_MINUTES * 60
    end_s = int(now.timestamp())
    queries = {
        "evidence-metrics.json": (
            "/api/datasources/proxy/uid/prometheus/api/v1/query",
            {"query": "order_lookup_requests_total"},
        ),
        "evidence-logs.json": (
            "/api/datasources/proxy/uid/loki/loki/api/v1/query_range",
            {
                "query": '{service_name="order-tracker"}',
                "limit": 200,
                "start": start_s * 1_000_000_000,
                "end": end_s * 1_000_000_000,
            },
        ),
        # No start/end here on purpose: with a time window Tempo only searches
        # flushed blocks, so traces still sitting in the ingester (the most
        # recent ones, i.e. the incident itself) would be missed.
        "evidence-traces.json": (
            "/api/datasources/proxy/uid/tempo/api/search",
            {"q": '{resource.service.name="order-tracker"}', "limit": 20},
        ),
    }
    for name, (path, params) in queries.items():
        result = grafana_get(path, params)
        if name == "evidence-traces.json":
            # Tempo search is eventually consistent right after ingestion/flush;
            # an empty-but-valid result is retried a couple of times.
            for _ in range(3):
                if result.get("error") or result.get("traces"):
                    break
                time.sleep(2)
                result = grafana_get(path, params)
        (incident_dir / name).write_text(json.dumps(result, indent=2))


def write_incident_md(incident_dir, alert):
    labels = alert.get("labels", {})
    annotations = alert.get("annotations", {})
    lines = [
        "# Incident",
        "",
        f"- alertname: {labels.get('alertname', 'unknown')}",
        f"- status: {alert.get('status', 'unknown')}",
        f"- endpoint: {labels.get('endpoint', 'n/a')}",
        f"- window: {labels.get('window', 'n/a')}",
        f"- severity: {labels.get('severity', 'n/a')}",
        f"- summary: {annotations.get('summary', 'n/a')}",
        f"- description: {annotations.get('description', 'n/a')}",
        f"- dashboard: {annotations.get('dashboard_url', 'n/a')}",
        f"- started at: {alert.get('startsAt', 'n/a')}",
        "",
        "Files: alert.json, evidence-metrics.json, evidence-logs.json,",
        "evidence-traces.json, agent-response.md (written by the on-call agent).",
    ]
    (incident_dir / "incident.md").write_text("\n".join(lines) + "\n")


def run_agent(incident_dir, alert):
    prompt = PROMPT_TEMPLATE.format(
        INCIDENT_DIR=incident_dir,
        REPO_DIR=REPO_DIR,
        ENDPOINT=alert.get("labels", {}).get("endpoint", "unknown"),
        SUMMARY=alert.get("annotations", {}).get("summary", "no summary"),
    )
    status_file = incident_dir / "agent-status.txt"
    status_file.write_text("running\n")
    try:
        proc = subprocess.run(
            shlex.split(AGENT_CMD) + [prompt],
            cwd=REPO_DIR,
            capture_output=True,
            text=True,
            timeout=AGENT_TIMEOUT,
        )
        output = ANSI_RE.sub("", proc.stdout or proc.stderr or "")
        (incident_dir / "agent-response.md").write_text(output)
        status_file.write_text(f"done exit={proc.returncode}\n")
    except subprocess.TimeoutExpired:
        (incident_dir / "agent-response.md").write_text("agent timed out\n")
        status_file.write_text("timeout\n")
    except Exception as exc:
        status_file.write_text(f"error: {type(exc).__name__}: {exc}\n")


def handle_alert(alert):
    now = datetime.now(timezone.utc)
    alertname = re.sub(r"[^A-Za-z0-9-]+", "-", alert.get("labels", {}).get("alertname", "alert")).strip("-")
    incident_dir = INCIDENTS_DIR / f"{now.strftime('%Y%m%dT%H%M%S')}-{alertname}"
    incident_dir.mkdir(parents=True, exist_ok=True)
    (incident_dir / "alert.json").write_text(json.dumps(alert, indent=2))
    try:
        gather_evidence(incident_dir, now)
    except Exception as exc:
        (incident_dir / "evidence-error.txt").write_text(f"{type(exc).__name__}: {exc}\n")
    write_incident_md(incident_dir, alert)
    if alert.get("status") == "firing":
        threading.Thread(target=run_agent, args=(incident_dir, alert), daemon=True).start()
    return incident_dir


class Responder(BaseHTTPRequestHandler):
    def _send(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != "/alerts":
            self._send(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length))
        except Exception as exc:
            self._send(400, {"error": f"invalid payload: {exc}"})
            return
        incidents = [str(handle_alert(a)) for a in payload.get("alerts", [])]
        self._send(202, {"status": "accepted", "incidents": incidents})

    def do_GET(self):
        if self.path == "/healthz":
            self._send(200, {"status": "ok"})
        elif self.path == "/incidents":
            items = []
            for path in sorted(INCIDENTS_DIR.glob("*/")) if INCIDENTS_DIR.exists() else []:
                status = (path / "agent-status.txt")
                items.append(
                    {
                        "incident": path.name,
                        "agent_status": status.read_text().strip() if status.exists() else "not started",
                    }
                )
            self._send(200, {"incidents": items})
        else:
            self._send(404, {"error": "not found"})

    def log_message(self, fmt, *args):
        print(f"[responder] {self.address_string()} {fmt % args}", flush=True)


if __name__ == "__main__":
    INCIDENTS_DIR.mkdir(exist_ok=True)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Responder)
    print(f"[responder] listening on 0.0.0.0:{PORT} (agent: {AGENT_CMD})", flush=True)
    server.serve_forever()
