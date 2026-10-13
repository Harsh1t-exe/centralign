"""Tool layer against the real sandbox in a real browser (no LLM involved)."""
import re

import pytest

from worker.events import Run
from worker.executor import ToolExecutor
from worker.interaction import AutoBroker
from worker.safety import ActionPolicy, SecretVault
from worker.tools import ALL_TOOLS
from worker.tools.base import ToolContext


@pytest.fixture
def make_executor(env, settings):
    contexts = []

    def make(autonomy="autonomous", answers=lambda item: ("ok", True)):
        settings.autonomy = autonomy
        run = Run(task="test", runs_dir=settings.runs_dir)
        ctx = ToolContext(run=run, env=env, settings=settings, policy=ActionPolicy(env, autonomy),
                          vault=SecretVault(env), broker=AutoBroker(run, answers))
        contexts.append(ctx)
        return ToolExecutor(ctx, ALL_TOOLS), ctx

    yield make
    for ctx in contexts:
        if ctx.browser:
            ctx.browser.close()


def ref(snapshot: str, pattern: str) -> str:
    m = re.search(r"\[(e\d+) " + pattern, snapshot)
    assert m, f"no element matching {pattern!r} in:\n{snapshot}"
    return m.group(1)


def test_snapshot_download_and_pdf(acme, make_executor):
    ex, ctx = make_executor()
    r = ex.execute("browser_navigate", {"url": "/mail"})
    assert not r.is_error and "Invoice GX-2026-1007" in r.content
    r = ex.execute("browser_click", {"ref": ref(r.content, r'link "Invoice GX-2026-1007')})
    assert "/mail/107" in r.content
    r = ex.execute("browser_click", {"ref": ref(r.content, r'link "📄 GX-2026-1007.pdf')})
    assert "Downloaded file: downloads/GX-2026-1007.pdf" in r.content
    r = ex.execute("read_file", {"path": "downloads/GX-2026-1007.pdf"})
    assert "TOTAL DUE USD 12,480.50" in r.content
    assert len(ctx.run.screenshots) == 3  # one per browser action


def test_secrets_are_substituted_and_redacted(acme, make_executor):
    ex, ctx = make_executor()
    r = ex.execute("browser_navigate", {"url": "/erp"})
    assert "Sign in" in r.content
    ex.execute("browser_type", {"ref": ref(r.content, r'input "Username"'), "text": "{{secret:erp_username}}"})
    r = ex.execute("browser_type", {"ref": ref(r.content, r'password "Password"'), "text": "{{secret:erp_password}}"})
    assert "sandbox-Pa55!" not in r.content
    r = ex.execute("browser_click", {"ref": ref(r.content, r'button "Sign in"')})
    assert "Accounts Payable" in r.content
    # the trace never contains secret values
    trace = (ctx.run.dir / "trace.jsonl").read_text(encoding="utf-8")
    assert "sandbox-Pa55!" not in trace and "{{secret:erp_password}}" in trace
    # API key substitution in headers; the echoed value is redacted
    r = ex.execute("http_request", {"method": "GET", "url": "/api/invoices",
                                    "headers": {"X-API-Key": "{{secret:ap_api_key}}"}})
    assert r.content.startswith("HTTP 200") and "GX-2026-0912" in r.content
    r = ex.execute("http_request", {"method": "GET", "url": "/api/invoices", "headers": {"X-API-Key": "{{secret:nope}}"}})
    assert r.is_error and "Unknown secret" in r.content


def test_network_policy_blocks_admin_and_external(acme, make_executor):
    ex, _ = make_executor()
    r = ex.execute("browser_navigate", {"url": "/admin/state"})
    assert r.is_error and "off-limits" in r.content
    r = ex.execute("http_request", {"method": "GET", "url": "https://example.com/"})
    assert r.is_error and "not an allowed origin" in r.content


def test_high_risk_action_requires_approval_even_when_autonomous(acme, make_executor):
    asked = []
    ex, ctx = make_executor(answers=lambda item: (asked.append(item.prompt) or "no", False))
    _login(ex)
    r = ex.execute("browser_navigate", {"url": "/erp/vendors/V-1003/bank"})
    ex.execute("browser_type", {"ref": ref(r.content, r'input "Account number"'), "text": "123"})
    r = ex.execute("browser_snapshot", {})
    r = ex.execute("browser_click", {"ref": ref(r.content, r'button "Save bank details"')})
    assert r.is_error and "did not approve" in r.content
    assert asked and "bank details" in asked[0].lower()
    assert acme.state()["vendors"][2]["bank"]["account"] == "****1180"  # unchanged


def test_supervised_mode_gates_ordinary_writes(acme, make_executor):
    ex, ctx = make_executor(autonomy="supervised", answers=lambda item: ("go ahead", True))
    _login(ex)  # sign-in is not a business write, so no approval needed
    assert ctx.run.interactions == []
    r = ex.execute("http_request", {"method": "POST", "url": "/api/invoices",
                                    "headers": {"X-API-Key": "{{secret:ap_api_key}}"},
                                    "json": {"vendor_id": "V-1004", "number": "HL-88213", "invoice_date": "2026-10-09",
                                             "due_date": "2026-10-24", "amount": "640.00", "currency": "USD"}})
    assert r.content.startswith("HTTP 201")
    assert len(ctx.run.interactions) == 1 and ctx.run.interactions[0]["approved"]
    assert ctx.run.actions[0]["approval"].startswith("approved by user")


def _login(ex):
    r = ex.execute("browser_navigate", {"url": "/erp/login"})
    ex.execute("browser_type", {"ref": ref(r.content, r'input "Username"'), "text": "{{secret:erp_username}}"})
    r = ex.execute("browser_type", {"ref": ref(r.content, r'password "Password"'), "text": "{{secret:erp_password}}",
                                    "press_enter": True})
    assert "Accounts Payable" in r.content, r.content
