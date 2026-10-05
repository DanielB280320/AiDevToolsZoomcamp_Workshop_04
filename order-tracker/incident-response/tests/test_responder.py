import json
import stat
import time

import pytest
from fastapi.testclient import TestClient

import responder

TEST_ALERT = {
    "status": "firing",
    "labels": {"alertname": "ResponderTest", "test": "true"},
    "annotations": {"summary": "Test notification; no incident to fix"},
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    fake_claude = tmp_path / "claude"
    fake_claude.write_text(
        "#!/bin/sh\n"
        "echo '{\"type\":\"system\"}'\n"
        "echo '{\"type\":\"result\",\"result\":\"Got it.\\\\nTEST: received the test alert.\"}'\n"
    )
    fake_claude.chmod(fake_claude.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(responder, "CLAUDE_BIN", str(fake_claude))
    monkeypatch.setattr(responder, "INCIDENTS_DIR", tmp_path / "incidents")
    # Nothing listens here, so context collection records errors instead of data.
    for name in ("PROMETHEUS_URL", "LOKI_URL", "TEMPO_URL"):
        monkeypatch.setattr(responder, name, "http://127.0.0.1:9")
    return TestClient(responder.app)


def wait_for(client, incident_id):
    for _ in range(100):
        incident = client.get(f"/incidents/{incident_id}").json()
        if incident.get("state", "").startswith("agent ") and incident["state"] != "agent running":
            return incident
        time.sleep(0.05)
    raise AssertionError(f"incident did not finish: {incident}")


def test_firing_alert_saves_context_and_runs_agent(client):
    response = client.post("/alerts", json={"alerts": [TEST_ALERT]})
    assert response.status_code == 202
    [result] = response.json()["incidents"]
    assert result["status"] == "investigating"

    incident = wait_for(client, result["incident"])
    assert incident["state"] == "agent finished"
    assert incident["last_line"] == "TEST: received the test alert."

    incident_dir = responder.INCIDENTS_DIR / result["incident"]
    assert json.loads((incident_dir / "alert.json").read_text()) == TEST_ALERT
    context = (incident_dir / "context.md").read_text()
    assert "ResponderTest" in context
    assert "## Collection errors" in context
    assert (incident_dir / "prompt.md").exists()


def test_resolved_alerts_are_ignored(client):
    response = client.post("/alerts", json={"alerts": [{**TEST_ALERT, "status": "resolved"}]})
    assert response.json()["incidents"][0]["status"] == "ignored"


def test_repeated_alert_while_investigating_starts_one_agent(client):
    fingerprint = responder.label_fingerprint(TEST_ALERT["labels"])
    responder.active[fingerprint] = "existing"
    try:
        response = client.post("/alerts", json={"alerts": [TEST_ALERT]})
    finally:
        responder.active.pop(fingerprint)
    assert response.json()["incidents"][0] == {
        "alert": "ResponderTest", "status": "already investigating", "incident": "existing",
    }


def test_context_uses_longest_stacktrace():
    alert = {"status": "firing", "labels": {"alertname": "X"}}
    context = {
        "route": None, "method": None, "errors": [], "metrics": [],
        "start": responder.datetime.now(responder.timezone.utc),
        "end": responder.datetime.now(responder.timezone.utc),
        "logs": [{"time": "t", "line": "GET /x -> 500",
                  "exception_stacktrace": "Traceback\n  frame\nValueError: day is out of range for month"}],
        "traces": [{"trace_id": "abc", "name": "GET /x",
                    "exceptions": [{"exception.stacktrace": "Traceback\n  fra"}]}],
    }
    assert "ValueError: day is out of range for month" in responder.render_context(alert, context)
