"""Acme Corp sandbox: the simulated company environment the AI worker operates in.

It is a normal server-rendered web app (no special hooks for the agent):
  /            intranet portal
  /mail        webmail inbox with vendor invoices (PDF attachments)
  /erp         accounts-payable system (login, vendors, invoices)
  /wiki        company policies and contacts
  /api         JSON API for the AP system (API key required)
  /admin       reset + fault injection, used by tests and demos (blocked for the agent)

Fault injection ("chaos") lets demos exercise the agent's failure handling:
  submit_fail_once      next invoice submission returns 503 and is NOT saved
  submit_lost_response  next invoice submission IS saved but the user sees a 504
  session_expire_after  ERP session expires after N authenticated page views
  latency_ms            artificial latency on every request
Set via env ACME_CHAOS="submit_fail_once,session_expire_after=8" or POST /admin/chaos.
"""
from __future__ import annotations

import os
import secrets
import threading
import time
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, Header, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import data
from .invoices_pdf import render_invoice_pdf

BASE = Path(__file__).parent
ERP_USERS = {"ap.agent": "sandbox-Pa55!", "dana.lee": "manager-Pa55!"}
API_KEY = "acme-sandbox-key-7f3a"
APPROVAL_THRESHOLD = Decimal("10000.00")
CURRENCIES = ["USD", "EUR", "GBP"]

app = FastAPI(title="Acme Corp Sandbox", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")

_lock = threading.RLock()
STATE: dict = data.fresh_state()
SESSIONS: dict[str, dict] = {}
CHAOS: dict = {}


def parse_chaos(spec: str) -> dict:
    out: dict = {}
    for part in filter(None, (p.strip() for p in spec.split(","))):
        key, _, val = part.partition("=")
        out[key] = int(val) if val.isdigit() else True
    return out


CHAOS.update(parse_chaos(os.environ.get("ACME_CHAOS", "")))


# ---------------------------------------------------------------- helpers

def vendor_by_id(vid: str) -> dict | None:
    return next((v for v in STATE["vendors"] if v["id"] == vid), None)


def invoice_view(inv: dict) -> dict:
    v = vendor_by_id(inv["vendor_id"]) or {"name": "?"}
    return {**inv, "ref": f"AP-{inv['id']:06d}", "vendor_name": v["name"]}


def render(request: Request, name: str, status_code: int = 200, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def audit(actor: str, action: str, detail: str) -> None:
    STATE["audit"].append({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "actor": actor, "action": action,
                           "detail": detail})


@app.middleware("http")
async def latency(request: Request, call_next):
    if CHAOS.get("latency_ms"):
        time.sleep(CHAOS["latency_ms"] / 1000)
    return await call_next(request)


def current_user(request: Request) -> str | None:
    """Returns the logged-in ERP user, enforcing the optional session-expiry fault."""
    token = request.cookies.get("erp_session")
    sess = SESSIONS.get(token or "")
    if not sess:
        return None
    sess["views"] += 1
    limit = CHAOS.get("session_expire_after")
    if limit and sess["views"] > limit:
        # Expire once (the fault is consumed) so a re-login recovers.
        SESSIONS.pop(token, None)
        CHAOS.pop("session_expire_after", None)
        return None
    return sess["user"]


def login_redirect(request: Request, expired: bool = False) -> RedirectResponse:
    nxt = quote(str(request.url.path) + (f"?{request.url.query}" if request.url.query else ""))
    flag = "&expired=1" if expired else ""
    return RedirectResponse(f"/erp/login?next={nxt}{flag}", status_code=303)


def validate_invoice(form: dict) -> tuple[dict, list[str]]:
    """Shared by the HTML form and the JSON API. Returns (clean_record, errors)."""
    errors: list[str] = []
    clean: dict = {}
    vendor = vendor_by_id(str(form.get("vendor_id", "")).strip())
    if not vendor:
        errors.append("Vendor is required and must be an existing vendor.")
    else:
        clean["vendor_id"] = vendor["id"]
    number = str(form.get("number", "")).strip()
    if not number:
        errors.append("Invoice number is required.")
    clean["number"] = number
    dates = {}
    for field, label in (("invoice_date", "Invoice date"), ("due_date", "Due date")):
        raw = str(form.get(field, "")).strip()
        try:
            dates[field] = date.fromisoformat(raw)
            clean[field] = dates[field].isoformat()
        except ValueError:
            errors.append(f"{label} must be a valid date in YYYY-MM-DD format (got '{raw}').")
    if len(dates) == 2 and dates["due_date"] < dates["invoice_date"]:
        errors.append("Due date cannot be before the invoice date.")
    raw_amount = str(form.get("amount", "")).strip()
    try:
        amount = Decimal(raw_amount)
        if amount <= 0 or amount.as_tuple().exponent < -2:
            raise InvalidOperation
        clean["amount"] = f"{amount:.2f}"
    except InvalidOperation:
        amount = None
        errors.append(f"Amount must be a positive number with at most 2 decimals, like 1234.56 "
                      f"(no currency symbols or thousands separators; got '{raw_amount}').")
    currency = str(form.get("currency", "USD")).strip().upper()
    if currency not in CURRENCIES:
        errors.append(f"Currency must be one of {', '.join(CURRENCIES)}.")
    clean["currency"] = currency
    clean["description"] = str(form.get("description", "")).strip()
    clean["po"] = str(form.get("po", "")).strip()
    clean["approved_by"] = str(form.get("approved_by", "")).strip()
    if amount is not None and amount > APPROVAL_THRESHOLD and not clean["approved_by"]:
        errors.append("Invoices over USD 10,000.00 require manager approval: fill in 'Approved by'.")
    if vendor and number:
        dup = next((i for i in STATE["invoices"] if i["vendor_id"] == vendor["id"]
                    and i["number"].lower() == number.lower() and i["status"] != "Void"), None)
        if dup:
            errors.append(f"Duplicate: invoice {number} already exists for {vendor['name']} "
                          f"(AP-{dup['id']:06d}).")
    return clean, errors


def create_invoice(clean: dict, actor: str) -> dict:
    inv = {"id": STATE["next_invoice_id"], **clean, "status": "Unpaid", "created_by": actor}
    STATE["next_invoice_id"] += 1
    STATE["invoices"].append(inv)
    audit(actor, "invoice.create", f"AP-{inv['id']:06d} {clean['number']} {clean['amount']} {clean['currency']}")
    return inv


# ---------------------------------------------------------------- portal / wiki

@app.get("/", response_class=HTMLResponse)
def portal(request: Request):
    unread = len(STATE["emails"])
    return render(request, "portal.html", unread=unread)


@app.get("/wiki", response_class=HTMLResponse)
def wiki_index(request: Request):
    return render(request, "wiki.html", page=None)


@app.get("/wiki/{slug}", response_class=HTMLResponse)
def wiki_page(request: Request, slug: str):
    pages = {
        "ap-policy": ("Accounts Payable Policy (AP-POL-004)", data.AP_POLICY),
        "contacts": ("Department contacts", """
            <table class="grid"><tr><th>Team</th><th>Email</th><th>Notes</th></tr>
            <tr><td>Accounts Payable</td><td>ap@acme.example</td><td>AP Manager: Dana Lee</td></tr>
            <tr><td>Procurement</td><td>procurement@acme.example</td><td>Vendor onboarding</td></tr>
            <tr><td>Security</td><td>security@acme.example</td><td>Report phishing / fraud</td></tr>
            <tr><td>Finance</td><td>priya.raman@acme.example</td><td>CFO</td></tr></table>"""),
    }
    if slug not in pages:
        return render(request, "message.html", status_code=404, title="Page not found",
                      message=f"No wiki page named '{slug}'.")
    title, html = pages[slug]
    return render(request, "wiki.html", page={"title": title, "html": html})


# ---------------------------------------------------------------- mail

@app.get("/mail", response_class=HTMLResponse)
def mail_index(request: Request, folder: str = "inbox", q: str = ""):
    items = STATE["emails"] if folder == "inbox" else STATE["sent"]
    if q:
        ql = q.lower()
        items = [m for m in items if ql in (m["subject"] + m["body"] + m.get("from_name", "") +
                                             m.get("from_addr", "") + m.get("to", "")).lower()]
    items = sorted(items, key=lambda m: (m["date"], m["id"]), reverse=True)
    return render(request, "mail_list.html", items=items, folder=folder, q=q)


@app.get("/mail/compose", response_class=HTMLResponse)
def mail_compose(request: Request, reply_to: int | None = None):
    to = subject = body = ""
    if reply_to:
        orig = next((m for m in STATE["emails"] if m["id"] == reply_to), None)
        if orig:
            to = orig["from_addr"]
            subject = "Re: " + orig["subject"]
            quoted = "\n".join("> " + line for line in orig["body"].splitlines())
            body = f"\n\nOn {orig['date']}, {orig['from_name']} wrote:\n{quoted}"
    return render(request, "mail_compose.html", to=to, subject=subject, body=body, error=None)


@app.post("/mail/send", response_class=HTMLResponse)
def mail_send(request: Request, to: str = Form(""), subject: str = Form(""), body: str = Form("")):
    if "@" not in to:
        return render(request, "mail_compose.html", status_code=422, to=to, subject=subject, body=body,
                      error="Please enter a valid recipient email address.")
    with _lock:
        msg = {"id": 900 + len(STATE["sent"]) + 1, "to": to, "subject": subject or "(no subject)",
               "body": body, "date": data.TODAY.isoformat(), "attachments": []}
        STATE["sent"].append(msg)
        audit("mail", "mail.send", f"to={to} subject={subject}")
    return RedirectResponse(f"/mail/sent/{msg['id']}?sent=1", status_code=303)


@app.get("/mail/sent/{msg_id}", response_class=HTMLResponse)
def mail_sent_view(request: Request, msg_id: int, sent: int = 0):
    msg = next((m for m in STATE["sent"] if m["id"] == msg_id), None)
    if not msg:
        return render(request, "message.html", status_code=404, title="Not found", message="Message not found.")
    return render(request, "mail_view.html", msg=msg, sent_folder=True, flash="Message sent." if sent else None)


@app.get("/mail/{msg_id}", response_class=HTMLResponse)
def mail_view(request: Request, msg_id: int):
    msg = next((m for m in STATE["emails"] if m["id"] == msg_id), None)
    if not msg:
        return render(request, "message.html", status_code=404, title="Not found", message="Message not found.")
    return render(request, "mail_view.html", msg=msg, sent_folder=False, flash=None)


@app.get("/mail/{msg_id}/attachments/{filename}")
def mail_attachment(msg_id: int, filename: str):
    msg = next((m for m in STATE["emails"] if m["id"] == msg_id), None)
    if not msg or filename not in msg["attachments"]:
        return Response("Attachment not found", status_code=404)
    doc = data.INVOICE_DOCS[filename.removesuffix(".pdf")]
    return Response(render_invoice_pdf(doc), media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# ---------------------------------------------------------------- ERP

@app.get("/erp/login", response_class=HTMLResponse)
def erp_login_form(request: Request, next: str = "/erp", expired: int = 0):
    msg = "Your session has expired. Please sign in again." if expired else None
    return render(request, "erp_login.html", next=next, error=None, info=msg)


@app.post("/erp/login", response_class=HTMLResponse)
def erp_login(request: Request, username: str = Form(""), password: str = Form(""), next: str = Form("/erp")):
    if ERP_USERS.get(username) != password:
        return render(request, "erp_login.html", status_code=401, next=next, info=None,
                      error="Invalid username or password.")
    token = secrets.token_hex(16)
    SESSIONS[token] = {"user": username, "views": 0}
    target = next if next.startswith("/erp") else "/erp"
    resp = RedirectResponse(target, status_code=303)
    resp.set_cookie("erp_session", token, httponly=True)
    return resp


@app.get("/erp/logout")
def erp_logout(request: Request):
    SESSIONS.pop(request.cookies.get("erp_session", ""), None)
    return RedirectResponse("/erp/login", status_code=303)


def _had_session(request: Request) -> bool:
    return bool(request.cookies.get("erp_session"))


@app.get("/erp", response_class=HTMLResponse)
def erp_home(request: Request):
    user = current_user(request)
    if not user:
        return login_redirect(request, expired=_had_session(request))
    unpaid = [i for i in STATE["invoices"] if i["status"] == "Unpaid"]
    total = sum(Decimal(i["amount"]) for i in unpaid)
    recent = [invoice_view(i) for i in sorted(STATE["invoices"], key=lambda i: i["id"], reverse=True)[:5]]
    return render(request, "erp_home.html", user=user, unpaid_count=len(unpaid), unpaid_total=f"{total:,.2f}",
                  recent=recent)


@app.get("/erp/vendors", response_class=HTMLResponse)
def erp_vendors(request: Request):
    user = current_user(request)
    if not user:
        return login_redirect(request, expired=_had_session(request))
    return render(request, "erp_vendors.html", user=user, vendors=STATE["vendors"])


@app.get("/erp/vendors/{vid}", response_class=HTMLResponse)
def erp_vendor(request: Request, vid: str, updated: int = 0):
    user = current_user(request)
    if not user:
        return login_redirect(request, expired=_had_session(request))
    v = vendor_by_id(vid)
    if not v:
        return render(request, "message.html", status_code=404, title="Not found", message="Vendor not found.")
    invs = [invoice_view(i) for i in STATE["invoices"] if i["vendor_id"] == vid]
    return render(request, "erp_vendor.html", user=user, v=v, invoices=invs,
                  flash="Bank details updated." if updated else None)


@app.get("/erp/vendors/{vid}/bank", response_class=HTMLResponse)
def erp_vendor_bank_form(request: Request, vid: str):
    user = current_user(request)
    if not user:
        return login_redirect(request, expired=_had_session(request))
    v = vendor_by_id(vid)
    return render(request, "erp_bank.html", user=user, v=v, error=None)


@app.post("/erp/vendors/{vid}/bank", response_class=HTMLResponse)
def erp_vendor_bank(request: Request, vid: str, bank: str = Form(""), account: str = Form(""),
                    routing: str = Form("")):
    user = current_user(request)
    if not user:
        return login_redirect(request, expired=_had_session(request))
    v = vendor_by_id(vid)
    with _lock:
        v["bank"] = {"bank": bank, "account": account, "routing": routing}
        audit(user, "vendor.bank_change", f"{vid} -> {bank} {account}")
    return RedirectResponse(f"/erp/vendors/{vid}?updated=1", status_code=303)


@app.get("/erp/invoices", response_class=HTMLResponse)
def erp_invoices(request: Request, vendor_id: str = "", status: str = "", q: str = ""):
    user = current_user(request)
    if not user:
        return login_redirect(request, expired=_had_session(request))
    items = [invoice_view(i) for i in STATE["invoices"]]
    if vendor_id:
        items = [i for i in items if i["vendor_id"] == vendor_id]
    if status:
        items = [i for i in items if i["status"].lower() == status.lower()]
    if q:
        ql = q.lower()
        items = [i for i in items if ql in (i["number"] + i["vendor_name"] + i["description"]).lower()]
    items.sort(key=lambda i: i["id"], reverse=True)
    return render(request, "erp_invoices.html", user=user, items=items, vendors=STATE["vendors"],
                  vendor_id=vendor_id, status=status, q=q)


def _new_form(request: Request, user: str, form: dict, errors: list[str], status_code: int = 200):
    return render(request, "erp_invoice_new.html", status_code=status_code, user=user, form=form, errors=errors,
                  vendors=STATE["vendors"], currencies=CURRENCIES)


@app.get("/erp/invoices/new", response_class=HTMLResponse)
def erp_invoice_new_form(request: Request):
    user = current_user(request)
    if not user:
        return login_redirect(request, expired=_had_session(request))
    return _new_form(request, user, {"currency": "USD"}, [])


@app.post("/erp/invoices/new", response_class=HTMLResponse)
async def erp_invoice_create(request: Request):
    user = current_user(request)
    if not user:
        return login_redirect(request, expired=True)
    form = dict(await request.form())
    with _lock:
        if CHAOS.pop("submit_fail_once", None):
            return render(request, "message.html", status_code=503, title="503 Service Unavailable",
                          message="The AP service is temporarily unavailable. Your changes were not saved. "
                                  "Please try again in a moment.")
        clean, errors = validate_invoice(form)
        if errors:
            return _new_form(request, user, form, errors, status_code=422)
        inv = create_invoice(clean, user)
        if CHAOS.pop("submit_lost_response", None):
            return render(request, "message.html", status_code=504, title="504 Gateway Timeout",
                          message="The server did not respond in time. The request may or may not have been "
                                  "processed.")
    return RedirectResponse(f"/erp/invoices/{inv['id']}?created=1", status_code=303)


@app.get("/erp/invoices/{inv_id}", response_class=HTMLResponse)
def erp_invoice(request: Request, inv_id: int, created: int = 0):
    user = current_user(request)
    if not user:
        return login_redirect(request, expired=_had_session(request))
    inv = next((i for i in STATE["invoices"] if i["id"] == inv_id), None)
    if not inv:
        return render(request, "message.html", status_code=404, title="Not found", message="Invoice not found.")
    flash = f"Invoice AP-{inv_id:06d} created successfully." if created else None
    return render(request, "erp_invoice.html", user=user, inv=invoice_view(inv), flash=flash)


@app.post("/erp/invoices/{inv_id}/void", response_class=HTMLResponse)
def erp_invoice_void(request: Request, inv_id: int):
    user = current_user(request)
    if not user:
        return login_redirect(request, expired=True)
    inv = next((i for i in STATE["invoices"] if i["id"] == inv_id), None)
    if inv:
        with _lock:
            inv["status"] = "Void"
            audit(user, "invoice.void", f"AP-{inv_id:06d}")
    return RedirectResponse(f"/erp/invoices/{inv_id}", status_code=303)


# ---------------------------------------------------------------- JSON API

def _api_auth(key: str | None) -> JSONResponse | None:
    if key != API_KEY:
        return JSONResponse({"error": "missing or invalid X-API-Key header"}, status_code=401)
    return None


@app.get("/api", response_class=HTMLResponse)
@app.get("/api/docs", response_class=HTMLResponse)
def api_docs(request: Request):
    return render(request, "api_docs.html")


@app.get("/api/vendors")
def api_vendors(x_api_key: str | None = Header(None)):
    if err := _api_auth(x_api_key):
        return err
    return [{k: v for k, v in vend.items() if k != "bank"} for vend in STATE["vendors"]]


@app.get("/api/invoices")
def api_invoices(vendor_id: str = "", number: str = "", status: str = "", x_api_key: str | None = Header(None)):
    if err := _api_auth(x_api_key):
        return err
    items = [invoice_view(i) for i in STATE["invoices"]]
    if vendor_id:
        items = [i for i in items if i["vendor_id"] == vendor_id]
    if number:
        items = [i for i in items if i["number"].lower() == number.lower()]
    if status:
        items = [i for i in items if i["status"].lower() == status.lower()]
    return items


@app.get("/api/invoices/{inv_id}")
def api_invoice(inv_id: int, x_api_key: str | None = Header(None)):
    if err := _api_auth(x_api_key):
        return err
    inv = next((i for i in STATE["invoices"] if i["id"] == inv_id), None)
    return invoice_view(inv) if inv else JSONResponse({"error": "not found"}, status_code=404)


@app.post("/api/invoices")
async def api_invoice_create(request: Request, x_api_key: str | None = Header(None)):
    if err := _api_auth(x_api_key):
        return err
    with _lock:
        clean, errors = validate_invoice(await request.json())
        if errors:
            return JSONResponse({"errors": errors}, status_code=422)
        inv = create_invoice(clean, "api")
    return JSONResponse(invoice_view(inv), status_code=201)


# ---------------------------------------------------------------- admin (tests / demo control)

@app.post("/admin/reset")
def admin_reset():
    with _lock:
        STATE.clear()
        STATE.update(data.fresh_state())
        SESSIONS.clear()
        CHAOS.clear()
    return {"ok": True}


@app.post("/admin/chaos")
async def admin_chaos(request: Request):
    body = await request.json()
    with _lock:
        CHAOS.clear()
        CHAOS.update(parse_chaos(body.get("spec", "")) if isinstance(body.get("spec"), str) else body)
    return {"chaos": CHAOS}


@app.get("/admin/state")
def admin_state():
    return {"invoices": [invoice_view(i) for i in STATE["invoices"]], "vendors": STATE["vendors"],
            "sent": STATE["sent"], "audit": STATE["audit"], "chaos": CHAOS}
