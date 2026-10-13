"""The free-tier backend adapter (Gemini/Groq/Ollama via the OpenAI-compatible API), tested offline
against a fake chat-completions server that behaves like Gemini."""
import json
import socket
import threading
import time
from dataclasses import replace

import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from worker.agent import WorkerAgent
from worker.events import Run
from worker.interaction import AutoBroker
from worker.llm import OpenAICompatLLM, to_openai_messages, to_openai_tools
from worker.tools import ALL_TOOLS


def test_tool_schemas_are_portable():
    tools, json_args = to_openai_tools([t.to_api() for t in ALL_TOOLS])
    http = next(t for t in tools if t["function"]["name"] == "http_request")["function"]["parameters"]
    assert http["properties"]["json"]["type"] == "string"          # free-form object -> JSON string
    assert json_args["http_request"] == {"headers", "params", "json"}
    assert "additionalProperties" not in json.dumps(tools)


def test_history_translation_and_elision():
    big = "Clicked e1\nPage changed\nURL: x\n" + "row\n" * 1000
    messages = [{"role": "user", "content": "Task"}]
    for i in range(3):
        messages.append({"role": "assistant", "content": [
            {"type": "thinking", "thinking": "", "signature": "s"},
            {"type": "text", "text": f"step {i}"},
            {"type": "tool_use", "id": f"c{i}", "name": "browser_click", "input": {"ref": "e1"},
             "provider_data": {"id": f"c{i}", "type": "function", "extra_content": {"google": {"thought_signature": f"sig{i}"}},
                               "function": {"name": "browser_click", "arguments": "{\"ref\": \"e1\"}"}}}]})
        messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": f"c{i}", "content": [
                {"type": "text", "text": big},
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}]},
            {"type": "text", "text": "Harness note"}]})
    out = to_openai_messages("SYS", messages, keep_full=1, images=True)
    roles = [m["role"] for m in out]
    assert roles[:3] == ["system", "user", "assistant"] and roles.count("tool") == 3
    assistants = [m for m in out if m["role"] == "assistant"]
    assert assistants[0]["tool_calls"][0]["extra_content"]["google"]["thought_signature"] == "sig0"
    tools = [m for m in out if m["role"] == "tool"]
    assert "omitted" in tools[0]["content"] and len(tools[0]["content"]) < 200     # old: elided
    assert tools[-1]["content"] == big                                              # latest: full
    images = [p for m in out if m["role"] == "user" and isinstance(m["content"], list)
              for p in m["content"] if p["type"] == "image_url"]
    assert len(images) == 1                                                         # only for the latest


class FakeGemini:
    """Scripted chat-completions server: rate-limits once, then plays tool calls, recording requests."""

    def __init__(self, turns):
        self.turns, self.requests, self.rate_limited = list(turns), [], False
        self.app = FastAPI()
        self.app.post("/v1/chat/completions")(self.handle)

    async def handle(self, req: Request):
        body = await req.json()
        if not self.rate_limited:
            self.rate_limited = True
            return JSONResponse({"error": {"message": "Resource exhausted", "code": 429}}, status_code=429,
                                headers={"retry-after": "0.1"})
        self.requests.append(body)
        name, args = self.turns.pop(0)
        n = len(self.requests)
        return {"id": f"r{n}", "object": "chat.completion", "created": 0, "model": body["model"],
                "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                    "role": "assistant", "content": f"Doing {name}.",
                    "tool_calls": [{"id": f"call{n}", "type": "function",
                                    "function": {"name": name, "arguments": json.dumps(args)},
                                    "extra_content": {"google": {"thought_signature": f"sig{n}"}}}]}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}}


@pytest.fixture
def fake_gemini(monkeypatch):
    servers = []

    def start(turns):
        fake = FakeGemini(turns)
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(fake.app, host="127.0.0.1", port=port, log_level="warning"))
        threading.Thread(target=server.run, daemon=True).start()
        time.sleep(0.8)
        servers.append(server)
        monkeypatch.setenv("GEMINI_API_KEY", "fake")
        monkeypatch.setenv("WORKER_BASE_URL", f"http://127.0.0.1:{port}/v1")
        monkeypatch.setenv("WORKER_MIN_CALL_INTERVAL", "0")
        return fake

    yield start
    for s in servers:
        s.should_exit = True


def test_agent_runs_end_to_end_through_adapter(acme, env, settings, fake_gemini):
    fake = fake_gemini([
        ("browser_navigate", {"url": "/wiki/ap-policy"}),
        ("http_request", {"method": "GET", "url": "/api/vendors",
                          "headers": json.dumps({"X-API-Key": "{{secret:ap_api_key}}"})}),
        ("finish", {"status": "completed", "summary": "Read the policy; 7 vendors.", "outcome": json.dumps({"vendors": 7})}),
    ])
    llm = OpenAICompatLLM("gemini", "gemini-3.8-flash", "high")
    run = Run(task="Read the AP policy and count vendors", runs_dir=settings.runs_dir)
    report = WorkerAgent(run, env, replace(settings, verify=False), llm, AutoBroker(run, lambda i: ("", None))).execute()

    assert report["status"] == "completed_unverified", report
    assert report["outcome"] == {"vendors": 7}                    # JSON-string arg decoded back to an object
    assert fake.rate_limited and len(fake.requests) == 3          # 429 was retried transparently
    last = fake.requests[-1]["messages"]
    echoed = [tc for m in last if m["role"] == "assistant" for tc in m.get("tool_calls", [])]
    assert [tc["extra_content"]["google"]["thought_signature"] for tc in echoed] == ["sig1", "sig2"]
    tool_msgs = [m["content"] for m in last if m["role"] == "tool"]
    assert "Approval thresholds" in tool_msgs[0] and tool_msgs[1].startswith("HTTP 200")
    assert fake.requests[0]["reasoning_effort"] == "high"
