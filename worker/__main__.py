"""Command line entry point.

  python -m worker "Find the latest invoice from Globex ..."     run one task in the terminal
  python -m worker serve --port 8001                              start the web UI
"""
from __future__ import annotations

import argparse
import json
import sys

from .config import DEFAULT_ENV_FILE, Environment, Settings


def print_event(e: dict) -> None:
    t, who = e["type"], ("[verifier] " if e.get("actor") == "verifier" else "")
    if t == "agent_note":
        print(f"\n  {who}💬 {e['text']}")
    elif t == "tool_call":
        badge = {"write": " [WRITE]", "high_risk": " [HIGH-RISK]"}.get(e["effect"], "")
        print(f"  {who}→ {e['tool']}{badge} {json.dumps(e['args'], ensure_ascii=False)[:160]}")
    elif t == "tool_result":
        first = e["preview"].splitlines()[0] if e["preview"] else ""
        print(f"  {who}{'✗' if e['is_error'] else '✓'} {first[:160]}")
    elif t == "plan_updated":
        marks = {"done": "x", "in_progress": ">", "skipped": "-", "blocked": "!"}
        print("  📋 Plan: " + " | ".join(f"[{marks.get(s['status'], ' ')}] {s['step']}" for s in e["plan"]))
    elif t == "memory_updated":
        print(f"  🧠 remember {e['key']} = {e['value']}")
    elif t == "verification_started":
        print("\n🔎 Independent verification started")
    elif t == "verification_result":
        print(f"🔎 Verification: {e['verdict'].upper()} — {e['summary']}")
    elif t == "error":
        print(f"  !! {e['message']}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m worker", description="Autonomous AI task worker")
    p.add_argument("task", nargs="+", help="Task in natural language, or 'serve' to start the web UI")
    p.add_argument("--env", default=str(DEFAULT_ENV_FILE), help="Environment profile (TOML)")
    p.add_argument("--autonomy", choices=["autonomous", "supervised"])
    p.add_argument("--headed", action="store_true", help="Show the browser window")
    p.add_argument("--no-verify", action="store_true", help="Skip independent verification")
    p.add_argument("--max-steps", type=int)
    p.add_argument("--port", type=int, default=8001)
    p.add_argument("--host", default="127.0.0.1", help="Bind address for 'serve' (0.0.0.0 in containers)")
    a = p.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # emoji-safe on Windows consoles

    if a.task == ["serve"]:
        import uvicorn

        from .server import create_app
        uvicorn.run(create_app(a.env), host=a.host, port=a.port, log_level="warning")
        return 0

    from .agent import WorkerAgent
    from .events import Run
    from .interaction import TerminalBroker
    from .llm import LLMError, make_llm
    from .report import to_markdown

    settings = Settings()
    if a.autonomy:
        settings.autonomy = a.autonomy
    if a.headed:
        settings.headless = False
    if a.no_verify:
        settings.verify = False
    if a.max_steps:
        settings.max_steps = a.max_steps
    env = Environment.load(a.env)
    try:
        llm = make_llm(settings)
    except LLMError as e:
        print(f"Error: {e}")
        return 2
    run = Run(task=" ".join(a.task), runs_dir=settings.runs_dir)
    run.subscribe(print_event)
    print(f"Run {run.id} · {env.name} · {settings.provider}/{settings.model} · {settings.autonomy}\nTask: {run.task}")
    report = WorkerAgent(run, env, settings, llm, TerminalBroker(run)).execute()
    print("\n" + "=" * 70 + "\n" + to_markdown(report))
    print(f"Full trace and evidence: {run.dir}")
    return 0 if report["status"] in ("completed_verified", "completed_unverified") else 1


if __name__ == "__main__":
    sys.exit(main())
