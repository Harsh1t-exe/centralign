"""Web backend: start a run, answer an approval through the API, stream events."""
import json
import time
from dataclasses import replace

from fastapi.testclient import TestClient

from worker.llm import ScriptedLLM, text, tool_use
from worker.server import create_app


def test_run_lifecycle_with_approval_over_http(acme, env, settings):
    script = lambda: ScriptedLLM([  # noqa: E731
        lambda m: [text("Need sign-off first."), tool_use("request_approval", action="Send the reply",
                                                          reason="Leaves the company")],
        lambda m: [tool_use("finish", status="completed", summary="Reply sent.")],
    ])
    app = create_app(env=env, settings_factory=lambda: replace(settings, verify=False),
                     llm_factory=lambda s: script())
    client = TestClient(app)
    run_id = client.post("/api/runs", json={"task": "Reply to Hooli"}).json()["id"]

    pending = []
    for _ in range(100):
        pending = client.get(f"/api/runs/{run_id}").json()["pending"]
        if pending:
            break
        time.sleep(0.05)
    assert pending and pending[0]["kind"] == "approval"
    r = client.post(f"/api/runs/{run_id}/interactions/{pending[0]['id']}", json={"answer": "ok", "approved": True})
    assert r.status_code == 200

    for _ in range(100):
        state = client.get(f"/api/runs/{run_id}").json()
        if state["status"] != "running":
            break
        time.sleep(0.05)
    assert state["report"]["status"] == "completed_unverified"
    assert state["report"]["interactions"][0]["approved"] is True

    with client.stream("GET", f"/api/runs/{run_id}/events") as stream:
        body = "".join(stream.iter_text())
    types = [json.loads(line[6:])["type"] for line in body.splitlines() if line.startswith("data: {")
             and line != "data: {}"]
    assert types[0] == "run_started" and "interaction_resolved" in types and types[-1] == "run_finished"
    assert client.get(f"/artifacts/{run_id}/report.md").status_code == 200


def test_access_code_and_single_run(acme, env, settings, monkeypatch):
    import threading
    gate = threading.Event()

    def slow_script(messages):
        gate.wait(10)  # keep the first run "running" until the test releases it
        return [tool_use("finish", status="failed", summary="stopped")]

    monkeypatch.setenv("WORKER_ACCESS_CODE", "s3cret")
    app = create_app(env=env, settings_factory=lambda: replace(settings, verify=False),
                     llm_factory=lambda s: ScriptedLLM([slow_script]))
    client = TestClient(app)
    assert client.get("/api/config").json()["access_code_required"] is True
    assert client.post("/api/runs", json={"task": "x"}).status_code == 401
    ok = {"X-Access-Code": "s3cret"}
    assert client.post("/api/runs", json={"task": "x"}, headers=ok).status_code == 200
    assert client.post("/api/runs", json={"task": "y"}, headers=ok).status_code == 409  # one at a time
    gate.set()
