"""The worker's control loop: think -> act -> observe, until the model calls finish.

The harness (not the model) owns budgets, stuck detection, policy, verification
and reporting, so those guarantees hold whatever the model does.
"""
from __future__ import annotations

import json
import traceback
from collections import deque

from .config import Environment, Settings
from .events import Run
from .executor import ToolExecutor, to_tool_result_block
from .interaction import InteractionBroker
from .llm import LLM, LLMError, LLMResponse
from .prompts import worker_system_prompt
from .report import build_report
from .safety import ActionPolicy, SecretVault
from .tools import ALL_TOOLS
from .tools.base import ToolContext, ToolResult

OBSERVATION_TOOLS = {"browser_snapshot", "browser_screenshot", "list_files", "update_plan", "remember"}


def llm_turn(llm: LLM, system: str, executor: ToolExecutor, messages: list[dict], run: Run, actor: str) -> LLMResponse:
    resp = llm.create(system, executor.api_tools(), messages)
    run.usage["llm_calls"] += 1
    for key, value in resp.usage.items():
        run.usage[key] = run.usage.get(key, 0) + (value or 0)
    for block in resp.content:
        if block.get("type") == "text" and block.get("text", "").strip():
            run.emit("agent_note", actor=actor, text=block["text"].strip())
        elif block.get("type") == "thinking" and block.get("thinking", "").strip():
            # With display="updates" these are the model's short progress notes, not raw reasoning.
            run.emit("agent_note", actor=actor, text=block["thinking"].strip(), kind="progress")
    return resp


def text_block(t: str) -> dict:
    return {"type": "text", "text": t}


class WorkerAgent:
    def __init__(self, run: Run, env: Environment, settings: Settings, llm: LLM, broker: InteractionBroker,
                 verifier_llm: LLM | None = None):
        self.run = run
        self.env = env
        self.settings = settings
        self.llm = llm
        self.verifier_llm = verifier_llm or llm
        self.ctx = ToolContext(run=run, env=env, settings=settings, policy=ActionPolicy(env, settings.autonomy),
                               vault=SecretVault(env), broker=broker)
        self.executor = ToolExecutor(self.ctx, ALL_TOOLS)
        self.system = worker_system_prompt(env)
        self.messages: list[dict] = []

    # ------------------------------------------------------------------ public
    def execute(self) -> dict:
        run = self.run
        run.emit("run_started", task=run.task, model=self.llm.model, autonomy=self.settings.autonomy,
                 environment=self.env.name)
        try:
            outcome = self._loop()
        except LLMError as e:
            outcome = {"status": "error", "summary": str(e)}
        except Exception as e:
            run.emit("error", message=f"{type(e).__name__}: {e}", traceback=traceback.format_exc())
            outcome = {"status": "error", "summary": f"Internal error: {type(e).__name__}: {e}"}
        finally:
            if self.ctx.browser:
                self.ctx.browser.close()
        report = build_report(run, outcome, self.llm.model)
        run.emit("run_finished", status=report["status"], summary=report["summary"])
        return report

    # ------------------------------------------------------------------ loop
    def _loop(self) -> dict:
        s, run = self.settings, self.run
        user = self.env.user.get("name", "the user")
        self.messages = [{"role": "user", "content": f"Task from {user}:\n{run.task}"}]
        recent: deque[str] = deque(maxlen=8)
        consecutive_errors = nudges = verification_rounds = 0
        warned_budget = False

        for step in range(1, s.max_steps + 1):
            run.emit("step", n=step, max=s.max_steps)
            resp = llm_turn(self.llm, self.system, self.executor, self.messages, run, "worker")
            self.messages.append({"role": "assistant", "content": resp.content})

            if resp.stop_reason == "refusal":
                return {"status": "failed", "summary": "The model declined to continue this task."}
            tool_uses = [b for b in resp.content if b.get("type") == "tool_use"]
            if not tool_uses:
                nudges += 1
                if nudges > 3:
                    last = " ".join(b.get("text", "") for b in resp.content if b.get("type") == "text")
                    return {"status": "failed", "summary": f"The agent stopped taking actions. Last message: {last}"}
                nudge = ("Your response hit the output limit; continue." if resp.stop_reason == "max_tokens" else
                         "You ended your turn without calling a tool. If the task is done, verify and call finish; "
                         "if you need input, call ask_user; otherwise continue working.")
                self.messages.append({"role": "user", "content": [text_block(nudge)]})
                continue

            results, notes = [], []
            for i, tu in enumerate(tool_uses):
                if resp.stop_reason == "max_tokens" and i == len(tool_uses) - 1:
                    result = ToolResult("This tool call was cut off by the output limit; issue it again.",
                                        is_error=True)
                else:
                    result = self.executor.execute(tu["name"], tu.get("input") or {}, tu["id"])
                results.append(to_tool_result_block(tu["id"], result))

                if tu["name"] not in OBSERVATION_TOOLS:
                    sig = tu["name"] + json.dumps(tu.get("input"), sort_keys=True)
                    recent.append(sig)
                    if recent.count(sig) == 3:
                        notes.append(f"Harness note: you have now repeated the same {tu['name']} call 3 times. "
                                     "It is not making progress; try a different approach, or ask the user / "
                                     "finish as blocked.")
                consecutive_errors = consecutive_errors + 1 if result.is_error else 0
                if consecutive_errors == 4:
                    notes.append("Harness note: the last 4 actions failed. Step back, re-read the current state, and "
                                 "reconsider your approach.")

            if not warned_budget and step >= int(s.max_steps * 0.8):
                warned_budget = True
                notes.append(f"Harness note: {step}/{s.max_steps} steps used. Wrap up: verify and finish, or finish "
                             "with an honest partial status.")

            if self.ctx.finished is not None:
                outcome = self.ctx.finished
                if outcome["status"] == "completed" and s.verify:
                    from .verifier import Verifier
                    verdict = Verifier(self.ctx, self.verifier_llm).verify(outcome)
                    outcome["verdict"] = verdict
                    if verdict["verdict"] == "fail" and verification_rounds < s.max_verification_rounds:
                        verification_rounds += 1
                        self.ctx.finished = None
                        notes.append("Independent verification FAILED, so the task is not finished. Findings:\n"
                                     + json.dumps(verdict, indent=1)
                                     + "\nInvestigate and fix the problems, then verify and call finish again. If "
                                       "they cannot be fixed, call finish with an honest status.")
                        self.messages.append({"role": "user", "content": results + [text_block(n) for n in notes]})
                        continue
                return outcome

            self.messages.append({"role": "user", "content": results + [text_block(n) for n in notes]})

        return {"status": "incomplete",
                "summary": f"Step budget ({s.max_steps}) exhausted before the task was finished."}
