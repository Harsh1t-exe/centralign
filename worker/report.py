"""Final report: a concise answer for the user plus the evidence behind it."""
from __future__ import annotations

import json
import time

from .events import Run
from .llm import estimate_cost


def final_status(outcome: dict) -> str:
    status = outcome.get("status", "error")
    if status != "completed":
        return status
    verdict = (outcome.get("verdict") or {}).get("verdict")
    return {"pass": "completed_verified", "fail": "failed_verification",
            "inconclusive": "completed_unverified", None: "completed_unverified"}[verdict]


def build_report(run: Run, outcome: dict, model: str) -> dict:
    report = {
        "run_id": run.id,
        "task": run.task,
        "status": final_status(outcome),
        "summary": outcome.get("summary", ""),
        "outcome": outcome.get("outcome", {}),
        "evidence": outcome.get("evidence", []),
        "agent_verification": outcome.get("verification", ""),
        "independent_verification": outcome.get("verdict"),
        "memory": run.memory,
        "plan": run.plan,
        "side_effects": run.actions,
        "interactions": run.interactions,
        "screenshots": [p.replace("\\", "/").split(f"{run.id}/", 1)[-1] for p in run.screenshots[-3:]],
        "model": model,
        "usage": run.usage,
        "estimated_cost_usd": estimate_cost(model, run.usage),
        "duration_s": round(time.time() - run.started_at, 1),
    }
    run.report = report
    run.status = report["status"]
    (run.dir / "report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    (run.dir / "report.md").write_text(to_markdown(report), encoding="utf-8")
    return report


def _result(action: dict) -> str:
    if action["ok"]:
        return "ok"
    return f"FAILED, HTTP {action['http_status']}" if action.get("http_status") else "FAILED"


def to_markdown(r: dict) -> str:
    lines = [f"# Task report: {r['status'].replace('_', ' ')}", "", f"**Task:** {r['task']}", "",
             "## Summary", r["summary"] or "_none_", ""]
    if r["outcome"]:
        lines += ["## Outcome", *[f"- **{k}:** {v}" for k, v in r["outcome"].items()], ""]
    v = r.get("independent_verification")
    if v:
        lines += [f"## Independent verification: {v['verdict'].upper()}", v.get("summary", "")]
        lines += [f"- [{c.get('result')}] {c.get('check')} — {c.get('evidence', '')}" for c in v.get("checks", [])]
        lines.append("")
    if r["side_effects"]:
        lines += ["## Changes made to external systems",
                  *[f"- {a['ts']} {a['description']} ({_result(a)})"
                    + (f" — {a['approval']}" if a.get("approval") else "") for a in r["side_effects"]], ""]
    if r["interactions"]:
        lines += ["## Questions & approvals",
                  *[f"- {i['kind']}: {i['prompt']} → {i['answer']}" for i in r["interactions"]], ""]
    if r["memory"]:
        lines += ["## Facts gathered", *[f"- {k}: {m['value']} _(source: {m['source'] or 'n/a'})_"
                                         for k, m in r["memory"].items()], ""]
    if r["evidence"] or r["screenshots"]:
        lines += ["## Evidence", *[f"- {e}" for e in r["evidence"]],
                  *[f"- screenshot: {s}" for s in r["screenshots"]], ""]
    cost = r.get("estimated_cost_usd")
    lines += ["---", f"Model {r['model']} · {r['usage'].get('llm_calls', 0)} LLM calls · "
                     f"{r['duration_s']}s" + (f" · ~${cost}" if cost is not None else "")]
    return "\n".join(lines) + "\n"
