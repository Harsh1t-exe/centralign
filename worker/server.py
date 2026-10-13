"""Web UI backend: start tasks, stream live events, answer questions/approvals."""
from __future__ import annotations

import asyncio
import json
import os
import threading
from pathlib import Path
from typing import Callable

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .agent import WorkerAgent
from .config import DEFAULT_ENV_FILE, Environment, Settings
from .events import Run
from .interaction import InteractionBroker
from .llm import LLM, LLMError, has_credentials, make_llm

UI_DIR = Path(__file__).parent / "ui"

EXAMPLES = [
    "Find the latest invoice from Globex, extract the amount and due date, enter it into our AP system, "
    "and tell me once it is done.",
    "Quarter-end close is coming. Make sure every vendor invoice we've received in the AP mailbox is entered in "
    "the AP system.",
    "Umbrella Supplies emailed us their new bank details. Please take care of it.",
    "Which unpaid invoices are due in the next 30 days? Save them as a CSV and tell me the total.",
    "Reply to Hooli confirming we received invoice HL-88213 and that it will be paid by its due date.",
]


class RunRequest(BaseModel):
    task: str
    autonomy: str | None = None


class Answer(BaseModel):
    answer: str = ""
    approved: bool | None = None


class ChaosRequest(BaseModel):
    spec: str = ""


def create_app(env_file: str | Path = DEFAULT_ENV_FILE, *, env: Environment | None = None,
               settings_factory: Callable[[], Settings] = Settings,
               llm_factory: Callable[[Settings], LLM] | None = None) -> FastAPI:
    env = env or Environment.load(env_file)
    llm_factory = llm_factory or make_llm
    app = FastAPI(title="AI Worker")
    runs: dict[str, tuple[Run, InteractionBroker]] = {}
    base_settings = settings_factory()
    base_settings.runs_dir.mkdir(parents=True, exist_ok=True)
    # Optional shared secret for public deployments, so strangers can't spend the model quota.
    access_code = os.environ.get("WORKER_ACCESS_CODE", "")

    def require_code(x_access_code: str | None = Header(None)) -> None:
        if access_code and x_access_code != access_code:
            raise HTTPException(401, "Access code required (ask the owner of this demo).")

    app.mount("/artifacts", StaticFiles(directory=base_settings.runs_dir), name="artifacts")

    @app.get("/")
    def index():
        return FileResponse(UI_DIR / "index.html")

    @app.get("/api/config")
    def config():
        return {"environment": env.name, "start_url": env.start_url, "model": base_settings.model,
                "provider": base_settings.provider,
                "autonomy": base_settings.autonomy, "examples": EXAMPLES,
                "has_api_key": has_credentials(base_settings.provider), "access_code_required": bool(access_code)}

    @app.post("/api/runs", dependencies=[Depends(require_code)])
    def start_run(req: RunRequest):
        if not req.task.strip():
            raise HTTPException(400, "Task is empty")
        if any(r.status == "running" for r, _ in runs.values()):
            # One shared sandbox and browser budget: run tasks one at a time.
            raise HTTPException(409, "Another task is still running. Please wait for it to finish.")
        settings = settings_factory()
        if req.autonomy:
            settings.autonomy = req.autonomy
        try:
            llm = llm_factory(settings)
        except LLMError as e:
            raise HTTPException(400, str(e))
        run = Run(task=req.task.strip(), runs_dir=settings.runs_dir)
        broker = InteractionBroker(run)
        runs[run.id] = (run, broker)
        agent = WorkerAgent(run, env, settings, llm, broker)
        threading.Thread(target=agent.execute, name=f"run-{run.id}", daemon=True).start()
        return {"id": run.id}

    @app.get("/api/runs")
    def list_runs():
        return [{"id": r.id, "task": r.task, "status": r.status} for r, _ in reversed(list(runs.values()))]

    def get(run_id: str) -> tuple[Run, InteractionBroker]:
        if run_id not in runs:
            raise HTTPException(404, "Unknown run")
        return runs[run_id]

    @app.get("/api/runs/{run_id}")
    def run_state(run_id: str):
        run, broker = get(run_id)
        return {"id": run.id, "task": run.task, "status": run.status, "plan": run.plan, "memory": run.memory,
                "report": run.report, "pending": [{"id": i.id, "kind": i.kind, "prompt": i.prompt,
                                                   "options": i.options, "details": i.details}
                                                  for i in broker.pending.values()]}

    @app.get("/api/runs/{run_id}/events")
    async def events(run_id: str, since: int = 0):
        run, _ = get(run_id)

        async def stream():
            i = since
            while True:
                while i < len(run.events):
                    event = run.events[i]
                    yield f"data: {json.dumps(event, default=str)}\n\n"
                    i += 1
                    if event["type"] == "run_finished":  # always the last event of a run
                        yield "event: end\ndata: {}\n\n"
                        return
                await asyncio.sleep(0.25)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/api/runs/{run_id}/interactions/{interaction_id}", dependencies=[Depends(require_code)])
    def answer(run_id: str, interaction_id: str, body: Answer):
        _, broker = get(run_id)
        if not broker.resolve(interaction_id, body.answer, body.approved):
            raise HTTPException(404, "No such pending interaction")
        return {"ok": True}

    # Demo controls for the human operator (the agent itself cannot reach /admin).
    @app.post("/api/sandbox/reset", dependencies=[Depends(require_code)])
    def sandbox_reset():
        return httpx.post(env.start_url.rstrip("/") + "/admin/reset", timeout=5).json()

    @app.post("/api/sandbox/chaos", dependencies=[Depends(require_code)])
    def sandbox_chaos(req: ChaosRequest):
        return httpx.post(env.start_url.rstrip("/") + "/admin/chaos", json={"spec": req.spec}, timeout=5).json()

    return app
