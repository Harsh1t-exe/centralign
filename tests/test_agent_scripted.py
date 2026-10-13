"""The full agent loop (planning, tools, approvals, failure recovery, verification,
reporting) against the real sandbox, with a scripted model in place of Claude."""
import re

from worker.agent import WorkerAgent
from worker.events import Run
from worker.interaction import AutoBroker
from worker.llm import ScriptedLLM, text, tool_use


def latest_snapshot(messages) -> str:
    for msg in reversed(messages):
        if msg["role"] == "user" and isinstance(msg["content"], list):
            for block in reversed(msg["content"]):
                if block.get("type") == "tool_result" and "\n---\n" in block["content"][0]["text"]:
                    return block["content"][0]["text"]
    return ""


def ref(messages, pattern):
    snap = latest_snapshot(messages)
    m = re.search(r"\[(e\d+) " + pattern, snap)
    assert m, f"no element matching {pattern!r} in:\n{snap}"
    return m.group(1)


def say(msg, name, **args):
    return lambda m: [text(msg), tool_use(name, **args)]


def fill_invoice_form(m):
    return [text("Filling in the invoice form."),
            tool_use("browser_select", ref=ref(m, 'select "Vendor"'), option="Globex Corporation (V-1001)"),
            tool_use("browser_type", ref=ref(m, 'input "Invoice number"'), text="GX-2026-1007"),
            tool_use("browser_type", ref=ref(m, 'input "Invoice date"'), text=INVOICE_DATE),
            tool_use("browser_type", ref=ref(m, 'input "Due date"'), text=DUE_DATE),
            tool_use("browser_type", ref=ref(m, 'input "Amount"'), text="12480.50"),
            tool_use("browser_type", ref=ref(m, r'input "Approved by'), text="Dana Lee")]


INVOICE_DATE = DUE_DATE = ""


def make_run(settings, task):
    return Run(task=task, runs_dir=settings.runs_dir)


def test_invoice_flow_with_approval_and_server_error(acme, env, settings):
    global INVOICE_DATE, DUE_DATE
    from acme.data import d
    INVOICE_DATE, DUE_DATE = d(-2), d(28)
    acme.chaos("submit_fail_once")  # first submission returns 503 and is not saved

    worker = ScriptedLLM([
        lambda m: [text("Planning."), tool_use("update_plan", steps=[
            {"step": "Find latest Globex invoice in mail", "status": "in_progress"},
            {"step": "Extract amount and due date"}, {"step": "Enter in AP system"}, {"step": "Verify"}])],
        say("Opening the mailbox.", "browser_navigate", url="/mail"),
        lambda m: [tool_use("browser_click", ref=ref(m, 'link "Invoice GX-2026-1007'))],
        lambda m: [tool_use("browser_click", ref=ref(m, r'link "📄 GX-2026-1007.pdf'))],
        say("Reading the PDF.", "read_file", path="downloads/GX-2026-1007.pdf"),
        lambda m: [tool_use("remember", key="amount", value="USD 12,480.50", source="GX-2026-1007.pdf"),
                   tool_use("remember", key="due_date", value=DUE_DATE, source="GX-2026-1007.pdf")],
        say("Opening the AP system.", "browser_navigate", url="/erp/invoices/new"),
        lambda m: [tool_use("browser_type", ref=ref(m, 'input "Username"'), text="{{secret:erp_username}}"),
                   tool_use("browser_type", ref=ref(m, 'password "Password"'), text="{{secret:erp_password}}",
                            press_enter=True)],
        say("Over the USD 10,000 threshold, so policy requires approval.", "request_approval",
            action="Enter Globex invoice GX-2026-1007 for USD 12,480.50", reason="AP-POL-004 §2"),
        fill_invoice_form,
        lambda m: [tool_use("browser_click", ref=ref(m, 'button "Save invoice"'))],
        # 503: check whether it was saved before retrying (it was not)
        say("Server error; checking if it was saved.", "browser_navigate", url="/erp/invoices?q=GX-2026-1007"),
        say("Not saved; retrying.", "browser_navigate", url="/erp/invoices/new"),
        fill_invoice_form,
        lambda m: [tool_use("browser_click", ref=ref(m, 'button "Save invoice"'))],
        say("Capturing evidence.", "browser_screenshot"),
        lambda m: [tool_use("finish", status="completed", summary="Entered GX-2026-1007 (USD 12,480.50, due "
                            f"{DUE_DATE}).", outcome={"ap_ref": "AP-000006"}, evidence=["/erp/invoices/6"],
                            verification="Reopened the saved record.")],
    ])
    verifier = ScriptedLLM([
        say("Checking the API.", "http_request", method="GET", url="/api/invoices",
            params={"number": "GX-2026-1007"}, headers={"X-API-Key": "{{secret:ap_api_key}}"}),
        lambda m: [tool_use("submit_verdict", verdict="pass", summary="Record exists with correct values.",
                            checks=[{"check": "amount", "result": "pass", "evidence": "12480.50"}])],
    ])
    run = make_run(settings, "Enter the latest Globex invoice")
    broker = AutoBroker(run, lambda item: ("approved", True))
    report = WorkerAgent(run, env, settings, worker, broker, verifier_llm=verifier).execute()

    assert report["status"] == "completed_verified", report
    matches = [i for i in acme.state()["invoices"] if i["number"] == "GX-2026-1007"]
    assert len(matches) == 1 and matches[0]["amount"] == "12480.50" and matches[0]["approved_by"] == "Dana Lee"
    # audit log: first submit failed (HTTP 503), second succeeded
    assert [a["ok"] for a in report["side_effects"]] == [False, True]
    assert report["side_effects"][0]["approval"] == "approved via request_approval"
    assert report["memory"]["amount"]["value"] == "USD 12,480.50"
    assert report["interactions"][0]["kind"] == "approval"
    assert (run.dir / "report.md").exists() and report["screenshots"]
    trace = (run.dir / "trace.jsonl").read_text(encoding="utf-8")
    assert "sandbox-Pa55!" not in trace


def test_failed_verification_sends_agent_back_to_work(acme, env, settings):
    worker = ScriptedLLM([
        say("Done, I think.", "finish", status="completed", summary="Entered the Hooli invoice."),
        lambda m: [text("Verification says nothing was entered. I can't fix it."),
                   tool_use("finish", status="failed", summary="Could not enter the invoice.")],
    ])
    verifier = ScriptedLLM([
        lambda m: [tool_use("submit_verdict", verdict="fail", summary="No HL-88213 invoice exists in the AP system.")],
    ])
    run = make_run(settings, "Enter the Hooli invoice")
    agent = WorkerAgent(run, env, settings, worker, AutoBroker(run, lambda i: ("", None)), verifier_llm=verifier)
    report = agent.execute()
    assert report["status"] == "failed"
    feedback = agent.messages[2]["content"][-1]["text"]
    assert "Independent verification FAILED" in feedback and "HL-88213" in feedback


def test_harness_handles_misbehaving_model(acme, env, settings):
    settings.max_steps = 7
    nav = lambda m: [tool_use("browser_navigate", url="/does-not-exist")]  # noqa: E731
    worker = ScriptedLLM([
        lambda m: [text("I'll just describe what to do instead of doing it.")],      # no tool -> nudge
        lambda m: [tool_use("teleport", where="erp")],                              # unknown tool
        lambda m: [tool_use("browser_click", ref="e999")],                          # stale ref
        lambda m: [tool_use("browser_type", ref="e1")],                             # invalid input
        nav, nav, nav,                                                              # repeated action
    ])
    run = make_run(settings, "Do something")
    agent = WorkerAgent(run, env, settings, worker, AutoBroker(run, lambda i: ("", None)))
    report = agent.execute()
    assert report["status"] == "incomplete"
    flat = str(agent.messages)
    assert "without calling a tool" in flat
    assert "Unknown tool 'teleport'" in flat
    assert "Unknown element ref 'e999'" in flat
    assert "Missing required argument(s): text" in flat
    assert "repeated the same browser_navigate call 3 times" in flat
    assert "HTTP status: 404" in flat
