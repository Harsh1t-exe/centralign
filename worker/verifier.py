"""Independent verification.

A second agent, with a fresh context and read-only tools, checks the real state of
the systems against the task. It sees the worker's claims only as claims to test.
Separating "doer" and "checker" catches the most common agent failure: reporting
success that did not happen (or happened wrongly).
"""
from __future__ import annotations

from dataclasses import replace

from .executor import ToolExecutor, to_tool_result_block
from .llm import LLM, LLMError
from .prompts import VERIFIER_SYSTEM_PROMPT, verifier_brief
from .safety import CONTROL
from .tools import ALL_TOOLS, READ_ONLY_TOOL_NAMES
from .tools.base import Action, Tool, ToolContext, ToolResult, schema


def _submit_verdict(ctx: ToolContext, args: dict) -> ToolResult:
    ctx.finished = {"verdict": args["verdict"], "summary": args["summary"], "checks": args.get("checks", [])}
    return ToolResult("Verdict recorded.")


SUBMIT_VERDICT = Tool(
    "submit_verdict", "Record your verification verdict. Call exactly once, at the end.",
    schema({"verdict": {"type": "string", "enum": ["pass", "fail", "inconclusive"]},
            "summary": {"type": "string"},
            "checks": {"type": "array", "items": {"type": "object", "properties": {
                "check": {"type": "string"}, "result": {"type": "string", "enum": ["pass", "fail", "unknown"]},
                "evidence": {"type": "string"}}, "required": ["check", "result"]}}},
           ["verdict", "summary"]),
    _submit_verdict, lambda ctx, a: Action(CONTROL))


class Verifier:
    def __init__(self, worker_ctx: ToolContext, llm: LLM, max_steps: int = 25):
        self.worker_ctx = worker_ctx
        self.llm = llm
        self.max_steps = max_steps
        # Same browser session (so sign-ins carry over), separate state, read-only executor.
        self.ctx = replace(worker_ctx, finished=None, approval_tokens=0)
        tools = [t for t in ALL_TOOLS if t.name in READ_ONLY_TOOL_NAMES] + [SUBMIT_VERDICT]
        self.executor = ToolExecutor(self.ctx, tools, read_only=True, actor="verifier")

    def verify(self, finish: dict) -> dict:
        from .agent import llm_turn, text_block

        run = self.ctx.run
        run.emit("verification_started")
        files = [p.relative_to(run.workspace).as_posix() for p in sorted(run.workspace.rglob("*")) if p.is_file()]
        messages = [{"role": "user", "content": verifier_brief(run.task, finish, run.memory, run.actions,
                                                               run.interactions, files)}]
        verdict = {"verdict": "inconclusive", "summary": "Verifier did not reach a verdict.", "checks": []}
        try:
            for _ in range(self.max_steps):
                resp = llm_turn(self.llm, VERIFIER_SYSTEM_PROMPT, self.executor, messages, run, "verifier")
                messages.append({"role": "assistant", "content": resp.content})
                tool_uses = [b for b in resp.content if b.get("type") == "tool_use"]
                if not tool_uses:
                    messages.append({"role": "user", "content": [text_block("Call submit_verdict to finish.")]})
                    continue
                results = [to_tool_result_block(tu["id"], self.executor.execute(tu["name"], tu.get("input") or {},
                                                                                tu["id"]))
                           for tu in tool_uses]
                if self.ctx.finished:
                    verdict = self.ctx.finished
                    break
                messages.append({"role": "user", "content": results})
        except LLMError as e:
            verdict = {"verdict": "inconclusive", "summary": f"Verification could not run: {e}", "checks": []}
        finally:
            if self.worker_ctx.browser is None:
                self.worker_ctx.browser = self.ctx.browser  # so the worker closes it
        run.emit("verification_result", **verdict)
        return verdict
