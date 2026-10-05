"""Incident responder for Order Tracker.

Receives Grafana alerts at POST /alerts, saves the context needed to understand the
problem (alert, metrics, error logs, traces) to incidents/<id>/, and starts Claude Code
in headless mode to investigate it.
"""

import hashlib
import json
import logging
import os
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException


HERE = Path(__file__).resolve().parent
APP_DIR = Path(os.getenv("APP_DIR", HERE.parent))
INCIDENTS_DIR = Path(os.getenv("INCIDENTS_DIR", HERE / "incidents"))
PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://localhost:9091")
LOKI_URL = os.getenv("LOKI_URL", "http://localhost:3101")
TEMPO_URL = os.getenv("TEMPO_URL", "http://localhost:3201")
SERVICE_NAME = os.getenv("SERVICE_NAME", "order-tracker")

CLAUDE_BIN = os.getenv("CLAUDE_BIN", "claude")
AGENT_ENABLED = os.getenv("AGENT_ENABLED", "true").lower() == "true"
AGENT_TIMEOUT = int(os.getenv("AGENT_TIMEOUT", "1800"))
# Enough to read code, edit it, run tests, rebuild the app, and repeat the request.
AGENT_ALLOWED_TOOLS = os.getenv(
    "AGENT_ALLOWED_TOOLS",
    "Read,Edit,Write,Glob,Grep,Bash(uv run *),Bash(docker compose *),Bash(curl *),"
    "Bash(git diff *),Bash(git status *),Bash(git log *)",
)

LOOKBACK = timedelta(minutes=10)
MAX_TRACES = 5

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("incident-response")

app = FastAPI(title="Incident Responder")

# Fingerprint -> incident id, so repeated notifications for one alert start one agent.
active: dict[str, str] = {}
active_lock = threading.Lock()


@app.get("/healthz")
def health():
    return {"status": "ok"}


@app.post("/alerts", status_code=202)
def receive_alerts(payload: dict):
    results = []
    for alert in payload.get("alerts", []):
        labels = alert.get("labels", {})
        fingerprint = alert.get("fingerprint") or label_fingerprint(labels)
        name = labels.get("alertname", "alert")
        if alert.get("status") != "firing":
            log.info("Ignoring %s alert %s", alert.get("status"), name)
            results.append({"alert": name, "status": "ignored", "reason": alert.get("status")})
            continue
        with active_lock:
            if fingerprint in active:
                results.append({"alert": name, "status": "already investigating", "incident": active[fingerprint]})
                continue
            incident_id = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{slug(name)}-{fingerprint[:8]}"
            active[fingerprint] = incident_id
        log.info("Opening incident %s", incident_id)
        threading.Thread(target=handle_incident, args=(alert, fingerprint, incident_id), daemon=True).start()
        results.append({"alert": name, "status": "investigating", "incident": incident_id})
    return {"incidents": results}


@app.get("/incidents")
def list_incidents():
    if not INCIDENTS_DIR.exists():
        return []
    return [read_status(path) for path in sorted(INCIDENTS_DIR.iterdir(), reverse=True) if path.is_dir()]


@app.get("/incidents/{incident_id}")
def get_incident(incident_id: str):
    path = INCIDENTS_DIR / incident_id
    if "/" in incident_id or not path.is_dir():
        raise HTTPException(404, "Incident not found")
    incident = read_status(path)
    response = path / "response.md"
    incident["response"] = response.read_text() if response.exists() else None
    return incident


def handle_incident(alert: dict, fingerprint: str, incident_id: str):
    incident_dir = INCIDENTS_DIR / incident_id
    try:
        incident_dir.mkdir(parents=True)
        write_json(incident_dir / "alert.json", alert)
        write_status(incident_dir, state="collecting")
        context = collect_context(alert, incident_dir)
        (incident_dir / "context.md").write_text(render_context(alert, context))
        if not AGENT_ENABLED:
            write_status(incident_dir, state="saved", note="AGENT_ENABLED=false")
            return
        run_agent(incident_dir)
    except Exception:
        log.exception("Incident %s failed", incident_id)
        write_status(incident_dir, state="failed")
    finally:
        with active_lock:
            active.pop(fingerprint, None)


# --- Context collection ---------------------------------------------------------------

def collect_context(alert: dict, incident_dir: Path) -> dict:
    labels = alert.get("labels", {})
    end = datetime.now(timezone.utc)
    start = min(parse_time(alert.get("startsAt")) or end, end) - LOOKBACK
    context = {
        "route": labels.get("http_route"),
        "method": labels.get("http_request_method"),
        "start": start,
        "end": end,
        "errors": [],
    }
    for name, collect in (("metrics", collect_metrics), ("logs", collect_logs), ("traces", collect_traces)):
        try:
            context[name] = collect(context, incident_dir)
        except Exception as exc:
            log.warning("Could not collect %s: %s", name, exc)
            context[name] = []
            context["errors"].append(f"{name}: {exc}")
    return context


def collect_metrics(context: dict, incident_dir: Path) -> list[dict]:
    query = (
        f'sum by (http_request_method, http_route, http_response_status_code) '
        f'(max_over_time(http_server_requests_total{{job="{SERVICE_NAME}"}}[15m]) '
        f'- min_over_time(http_server_requests_total{{job="{SERVICE_NAME}"}}[15m]))'
    )
    response = httpx.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": query}, timeout=10)
    response.raise_for_status()
    rows = [
        {**row["metric"], "requests_15m": round(float(row["value"][1]))}
        for row in response.json()["data"]["result"]
    ]
    rows = [row for row in rows if row["requests_15m"] > 0]
    write_json(incident_dir / "metrics.json", {"query": query, "result": rows})
    return rows


def collect_logs(context: dict, incident_dir: Path) -> list[dict]:
    query = f'{{service_name="{SERVICE_NAME}"}} | severity_text="ERROR"'
    if context["route"]:
        query += f' | http_route="{context["route"]}"'
    response = httpx.get(
        f"{LOKI_URL}/loki/api/v1/query_range",
        params={
            "query": query,
            "start": to_ns(context["start"]),
            "end": to_ns(context["end"]),
            "limit": 50,
            "direction": "backward",
        },
        timeout=10,
    )
    response.raise_for_status()
    entries = [
        {"time": from_ns(ts).isoformat(), "line": line, **stream["stream"]}
        for stream in response.json()["data"]["result"]
        for ts, line in stream["values"]
    ]
    entries.sort(key=lambda entry: entry["time"], reverse=True)
    write_json(incident_dir / "logs.json", {"query": query, "entries": entries})
    return entries


def collect_traces(context: dict, incident_dir: Path) -> list[dict]:
    conditions = [f'resource.service.name = "{SERVICE_NAME}"', "status = error"]
    if context["route"]:
        conditions.append(f'span.http.route = "{context["route"]}"')
    traceql = "{ " + " && ".join(conditions) + " }"
    response = httpx.get(
        f"{TEMPO_URL}/api/search",
        params={
            "q": traceql,
            "start": int(context["start"].timestamp()),
            "end": int(context["end"].timestamp()) + 1,
            "limit": MAX_TRACES,
        },
        timeout=10,
    )
    response.raise_for_status()
    traces_dir = incident_dir / "traces"
    traces_dir.mkdir(exist_ok=True)
    traces = []
    for found in response.json().get("traces", [])[:MAX_TRACES]:
        trace_id = found["traceID"]
        trace = httpx.get(f"{TEMPO_URL}/api/traces/{trace_id}", timeout=10)
        trace.raise_for_status()
        write_json(traces_dir / f"{trace_id}.json", trace.json())
        traces.append({"trace_id": trace_id, "name": found.get("rootTraceName"),
                       "exceptions": span_exceptions(trace.json())})
    write_json(incident_dir / "traces.json", {"query": traceql, "traces": traces})
    return traces


def span_exceptions(trace: dict) -> list[dict]:
    exceptions = []
    for batch in trace.get("batches", trace.get("resourceSpans", [])):
        for scope in batch.get("scopeSpans", []):
            for span in scope.get("spans", []):
                for event in span.get("events", []):
                    if event.get("name") == "exception":
                        exceptions.append({
                            attribute["key"]: next(iter(attribute["value"].values()), None)
                            for attribute in event.get("attributes", [])
                        })
    return exceptions


def render_context(alert: dict, context: dict) -> str:
    labels = alert.get("labels", {})
    annotations = alert.get("annotations", {})
    endpoint = f"{context['method'] or ''} {context['route'] or ''}".strip() or "(not in alert labels)"
    lines = [
        f"# Incident: {labels.get('alertname', 'alert')}",
        "",
        f"- Status: {alert.get('status')}",
        f"- Affected endpoint: {endpoint}",
        f"- Started: {alert.get('startsAt', 'unknown')}",
        f"- Context window: {context['start']:%Y-%m-%d %H:%M:%S} to {context['end']:%H:%M:%S} UTC",
        f"- Labels: {json.dumps(labels)}",
    ]
    lines += [f"- {key}: {value}" for key, value in annotations.items() if not key.startswith("__")]
    for key in ("dashboardURL", "panelURL", "generatorURL"):
        if alert.get(key):
            lines.append(f"- {key}: {alert[key]}")

    lines += ["", "## Requests in the last 15 minutes (Prometheus)", ""]
    for row in context["metrics"]:
        lines.append(f"- {row.get('http_request_method')} {row.get('http_route')} "
                     f"-> {row.get('http_response_status_code')}: {row['requests_15m']}")
    if not context["metrics"]:
        lines.append("- none found")

    lines += ["", "## Error logs (Loki, newest first)", ""]
    for entry in context["logs"][:20]:
        lines.append(f"- {entry['time']} trace_id={entry.get('trace_id', '-')} {entry['line']}")
    if not context["logs"]:
        lines.append("- none found")

    lines += ["", "## Failing traces (Tempo)", ""]
    for trace in context["traces"]:
        lines.append(f"- {trace['trace_id']} {trace['name']}")
    if not context["traces"]:
        lines.append("- none found")

    # Tempo truncates long span attributes, so use the longest copy (usually Loki's).
    stacktraces = [entry.get("exception_stacktrace") for entry in context["logs"]]
    stacktraces += [exc.get("exception.stacktrace") for trace in context["traces"] for exc in trace["exceptions"]]
    stacktrace = max(filter(None, stacktraces), key=len, default=None)
    if stacktrace:
        lines += ["", "## Stack trace (first failing request)", "", "```", stacktrace.strip(), "```"]

    if context["errors"]:
        lines += ["", "## Collection errors", ""] + [f"- {error}" for error in context["errors"]]
    return "\n".join(lines) + "\n"


# --- Agent ----------------------------------------------------------------------------

PROMPT = """You are the on-call engineer for Order Tracker. Grafana fired an alert, and the
incident responder saved its context in {incident_dir}:

- context.md: summary of the alert, affected endpoint, request counts, error logs, and stack trace
- alert.json, metrics.json, logs.json, traces.json, traces/*.json: raw data

The app's code is in the current directory (app/, tests/). It runs with Docker Compose as
service `app` at http://localhost:8000.

First decide whether this is a real incident. If the alert has the label test="true", or names
no affected endpoint, it is a test notification: do not change any files, and reply in two or
three sentences confirming what you received.

For a real incident:
1. Find the root cause using the logs, traces, and code.
2. Fix it with the smallest correct change, and add a regression test in tests/.
3. Run `uv run --frozen pytest -q`.
4. Rebuild and restart the app: `docker compose up --build -d --wait app`.
5. Repeat the failing request and confirm it no longer returns a 5xx response.
If you cannot fix it safely, do not guess: explain what you found and what a developer should check.

End your answer with exactly one final line in one of these forms:
TEST: <one-sentence acknowledgement>
RESOLVED: <one-sentence root cause and fix>
ESCALATE: <one-sentence reason>
"""


def run_agent(incident_dir: Path):
    prompt = PROMPT.format(incident_dir=incident_dir)
    (incident_dir / "prompt.md").write_text(prompt)
    command = [
        CLAUDE_BIN, "-p", prompt,
        "--output-format", "stream-json", "--verbose",
        "--permission-mode", "acceptEdits",
        # No MCP servers: the unattended agent only needs local files and the allowed commands.
        "--strict-mcp-config",
        "--allowedTools", AGENT_ALLOWED_TOOLS,
    ]
    write_status(incident_dir, state="agent running")
    log.info("Starting Claude Code for %s", incident_dir.name)
    with open(incident_dir / "transcript.jsonl", "w") as transcript:
        try:
            result = subprocess.run(command, cwd=APP_DIR, env=agent_env(), stdout=transcript,
                                    stderr=subprocess.STDOUT, timeout=AGENT_TIMEOUT)
            exit_code = result.returncode
        except subprocess.TimeoutExpired:
            exit_code = None
    response = final_result(incident_dir / "transcript.jsonl")
    (incident_dir / "response.md").write_text(response or "(the agent produced no result)\n")
    state = "agent finished" if exit_code == 0 else "agent timed out" if exit_code is None else "agent failed"
    last_line = response.strip().splitlines()[-1] if response and response.strip() else None
    write_status(incident_dir, state=state, exit_code=exit_code, last_line=last_line)
    log.info("%s: %s | %s", incident_dir.name, state, last_line)


def agent_env() -> dict:
    # Drop variables of any Claude Code session that started the responder, so the agent
    # runs as an independent session.
    return {
        key: value for key, value in os.environ.items()
        if not (key == "CLAUDECODE" or key.startswith("CLAUDE_CODE_") or key in {"CLAUDE_PID", "CLAUDE_EFFORT"})
    }


def final_result(transcript: Path) -> str | None:
    result = None
    for line in transcript.read_text().splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "result":
            result = event.get("result")
    return result


# --- Helpers --------------------------------------------------------------------------

def write_status(incident_dir: Path, **fields):
    path = incident_dir / "status.json"
    status = json.loads(path.read_text()) if path.exists() else {"incident": incident_dir.name}
    status.update(fields, updated_at=datetime.now(timezone.utc).isoformat())
    write_json(path, status)


def read_status(incident_dir: Path) -> dict:
    path = incident_dir / "status.json"
    return json.loads(path.read_text()) if path.exists() else {"incident": incident_dir.name}


def write_json(path: Path, data):
    path.write_text(json.dumps(data, indent=2, default=str) + "\n")


def label_fingerprint(labels: dict) -> str:
    return hashlib.sha256(json.dumps(labels, sort_keys=True).encode()).hexdigest()[:16]


def slug(text: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in text.lower()).strip("-")[:40] or "alert"


def parse_time(value: str | None) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value) if value else None
    except ValueError:
        return None
    # Grafana sends 0001-01-01 for unset times.
    return parsed if parsed and parsed.year > 1 else None


def to_ns(moment: datetime) -> int:
    return int(moment.timestamp() * 1_000_000_000)


def from_ns(value: str) -> datetime:
    return datetime.fromtimestamp(int(value) / 1_000_000_000, timezone.utc)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=os.getenv("RESPONDER_HOST", "127.0.0.1"), port=int(os.getenv("RESPONDER_PORT", "8001")))
