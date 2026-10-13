"""The single choke point every tool call passes through.

validate -> substitute secrets -> classify side effect -> policy / approval gate
-> run -> redact secrets -> record (trace, audit log of side effects)
"""
from __future__ import annotations

import base64
import json
import time
from pathlib import Path

from .safety import CONTROL, HIGH_RISK, READ, WRITE, SecretError
from .tools.base import Action, Tool, ToolContext, ToolError, ToolResult, validate_input

PREVIEW = 2_000


class ToolExecutor:
    def __init__(self, ctx: ToolContext, tools: list[Tool], read_only: bool = False, actor: str = "worker"):
        self.ctx = ctx
        self.tools = {t.name: t for t in tools}
        self.read_only = read_only
        self.actor = actor

    def api_tools(self) -> list[dict]:
        return [t.to_api() for t in self.tools.values()]

    def execute(self, name: str, args: dict, call_id: str = "") -> ToolResult:
        ctx, run = self.ctx, self.ctx.run
        tool = self.tools.get(name)
        if tool is None:
            return ToolResult(f"Unknown tool '{name}'. Available: {', '.join(self.tools)}", is_error=True)
        if err := validate_input(tool, args):
            return ToolResult(f"Invalid input for {name}: {err}", is_error=True)
        try:
            real_args = ctx.vault.substitute(args)
        except SecretError as e:
            return ToolResult(str(e), is_error=True)

        try:
            action = tool.describe(ctx, real_args)
        except Exception:
            action = Action(WRITE, f"{name} (unclassified)")  # fail closed
        action.effect = ctx.policy.escalate(action.effect, action.description)
        action.description = ctx.vault.redact(action.description)

        if self.read_only and action.effect not in (READ, CONTROL):
            run.emit("tool_blocked", actor=self.actor, tool=name, args=args, reason="read-only")
            return ToolResult(f"Blocked: you are read-only and this action would {action.description}.",
                              is_error=True)

        approval = self._gate(name, args, action)
        if approval is False:
            return ToolResult(f"Action not performed: the user did not approve '{action.description}'. "
                              f"Respect this decision; adjust your plan or report back.", is_error=True)

        run.emit("tool_call", actor=self.actor, id=call_id, tool=name, args=args, effect=action.effect,
                 description=action.description)
        started = time.time()
        try:
            result = tool.handler(ctx, real_args)
        except ToolError as e:
            result = ToolResult(str(e), is_error=True)
        except Exception as e:  # unexpected tool crash: report to the model, keep the run alive
            first_line = (str(e).strip().splitlines() or [""])[0][:500]
            result = ToolResult(f"{name} failed unexpectedly: {type(e).__name__}: {first_line}", is_error=True)
        result.content = ctx.vault.redact(result.content)

        if action.effect in (WRITE, HIGH_RISK):
            run.actions.append({"actor": self.actor, "tool": name, "description": action.description,
                                "effect": action.effect,
                                "ok": not result.is_error and result.data.get("http_status", 200) < 400,
                                "http_status": result.data.get("http_status"),
                                "approval": approval or None, "ts": time.strftime("%H:%M:%S"),
                                "url_after": result.data.get("url")})
        run.emit("tool_result", actor=self.actor, id=call_id, tool=name, is_error=result.is_error,
                 duration_s=round(time.time() - started, 2), preview=result.content[:PREVIEW],
                 data={k: (str(Path(v).relative_to(run.dir).as_posix()) if k == "screenshot" else v)
                       for k, v in result.data.items()})
        return result

    def _gate(self, name: str, args: dict, action: Action) -> str | bool | None:
        """Returns None (no approval needed), an approval note, or False (denied)."""
        ctx = self.ctx
        if action.effect not in (WRITE, HIGH_RISK):
            return None
        if ctx.approval_tokens > 0:
            # The agent asked for (and got) approval right before this action.
            ctx.approval_tokens -= 1
            return "approved via request_approval"
        if not ctx.policy.needs_approval(action.effect):
            return None
        reason = ("High-risk action (matches a high-risk pattern)" if action.effect == HIGH_RISK
                  else "Supervised mode: external changes need approval")
        details = f"{reason}.\nTool: {name}\nArguments: {json.dumps(args)[:800]}"
        item = ctx.broker.ask("approval", f"The agent wants to {action.description}", details=details)
        return f"approved by user: {item.answer}" if item.approved else False


def to_tool_result_block(call_id: str, result: ToolResult) -> dict:
    content: list[dict] = [{"type": "text", "text": result.content or "(no output)"}]
    for img in result.images:
        data = base64.standard_b64encode(Path(img).read_bytes()).decode()
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}})
    block = {"type": "tool_result", "tool_use_id": call_id, "content": content}
    if result.is_error:
        block["is_error"] = True
    return block
