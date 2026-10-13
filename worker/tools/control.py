"""Control tools: planning, working memory, talking to the user, and finishing."""
from __future__ import annotations

import time

from ..safety import CONTROL
from .base import Action, Tool, ToolContext, ToolError, ToolResult, schema

CONTROL_ACTION = lambda ctx, a: Action(CONTROL)  # noqa: E731
STATUSES = ["pending", "in_progress", "done", "skipped", "blocked"]


def update_plan(ctx: ToolContext, args: dict) -> ToolResult:
    steps = []
    for item in args["steps"]:
        if not isinstance(item, dict) or "step" not in item:
            raise ToolError("Each plan item must be an object like {\"step\": \"...\", \"status\": \"pending\"}.")
        status = item.get("status", "pending")
        if status not in STATUSES:
            raise ToolError(f"Invalid status '{status}'. Use one of {STATUSES}.")
        steps.append({"step": str(item["step"]), "status": status})
    ctx.run.plan = steps
    ctx.run.emit("plan_updated", plan=steps, note=args.get("note", ""))
    done = sum(s["status"] == "done" for s in steps)
    return ToolResult(f"Plan saved ({done}/{len(steps)} done).")


def remember(ctx: ToolContext, args: dict) -> ToolResult:
    key = args["key"].strip()
    ctx.run.memory[key] = {"value": args["value"], "source": args.get("source", ""), "ts": time.strftime("%H:%M:%S")}
    ctx.run.emit("memory_updated", key=key, value=args["value"], source=args.get("source", ""))
    facts = "\n".join(f"- {k}: {v['value']}  (source: {v['source'] or 'n/a'})" for k, v in ctx.run.memory.items())
    return ToolResult(f"Saved. Working memory now:\n{facts}")


def ask_user(ctx: ToolContext, args: dict) -> ToolResult:
    item = ctx.broker.ask("question", args["question"], args.get("options"), args.get("context", ""))
    return ToolResult(f"User answered: {item.answer}")


def request_approval(ctx: ToolContext, args: dict) -> ToolResult:
    details = args.get("details", "")
    if args.get("reason"):
        details = f"Why approval is needed: {args['reason']}\n{details}".strip()
    item = ctx.broker.ask("approval", args["action"], details=details)
    approver = ctx.env.user.get("name", "the user")
    role = ctx.env.user.get("role", "")
    who = f"{approver} ({role})" if role else approver
    if item.approved:
        ctx.approval_tokens += 1
        return ToolResult(f"APPROVED by {who}. Comment: {item.answer}. "
                          f"You may now perform exactly the approved action.")
    return ToolResult(f"DENIED by {who}. Comment: {item.answer}. Do not perform this action; adapt or report back.")


def finish(ctx: ToolContext, args: dict) -> ToolResult:
    ctx.finished = {
        "status": args["status"],
        "summary": args["summary"],
        "outcome": args.get("outcome", {}),
        "evidence": args.get("evidence", []),
        "verification": args.get("verification", ""),
    }
    ctx.run.emit("finish_requested", **ctx.finished)
    return ToolResult("Finish recorded.")


TOOLS = [
    Tool("update_plan",
         "Create or update your step-by-step plan for the task. Call it first, and again whenever a step "
         "completes or the plan changes.",
         schema({"steps": {"type": "array", "items": {"type": "object", "properties": {
             "step": {"type": "string"}, "status": {"type": "string", "enum": STATUSES}}, "required": ["step"]}},
             "note": {"type": "string"}}, ["steps"]), update_plan, CONTROL_ACTION),
    Tool("remember",
         "Store a fact you discovered (e.g. an extracted value, an ID, a decision) in working memory, with where "
         "it came from. Memory is shown to the user and used in the final report.",
         schema({"key": {"type": "string"}, "value": {"type": "string"},
                 "source": {"type": "string", "description": "Where the fact came from (URL, file, user)."}},
                ["key", "value"]), remember, CONTROL_ACTION),
    Tool("ask_user",
         "Ask the user a clarifying question and wait for the answer. Only for information you cannot find "
         "yourself or genuinely ambiguous choices with materially different outcomes.",
         schema({"question": {"type": "string"}, "options": {"type": "array", "items": {"type": "string"}},
                 "context": {"type": "string", "description": "What you found that makes this necessary."}},
                ["question"]), ask_user, CONTROL_ACTION),
    Tool("request_approval",
         "Ask the user to approve a specific action before you perform it (required by policy, high-risk, "
         "irreversible, or outside what the user asked). Describe exactly what you will do.",
         schema({"action": {"type": "string", "description": "The exact action you want to take."},
                 "reason": {"type": "string"},
                 "details": {"type": "string", "description": "Key data the approver needs (amounts, IDs...)."}},
                ["action", "reason"]), request_approval, CONTROL_ACTION),
    Tool("finish",
         "End the task. Call exactly once, after verifying the outcome. status: completed (goal achieved and "
         "verified), blocked (cannot proceed safely; explain), needs_user_action (a human must do something), "
         "failed (could not do it).",
         schema({"status": {"type": "string", "enum": ["completed", "blocked", "needs_user_action", "failed"]},
                 "summary": {"type": "string", "description": "Concise answer for the user: what was done and the "
                                                              "result."},
                 "outcome": {"type": "object", "description": "Key result fields (IDs, amounts, dates...)."},
                 "evidence": {"type": "array", "items": {"type": "string"},
                              "description": "URLs, record IDs, screenshot paths or files proving the outcome."},
                 "verification": {"type": "string", "description": "How you confirmed the outcome."}},
                ["status", "summary"]), finish, CONTROL_ACTION),
]
