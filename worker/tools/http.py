"""HTTP tool for JSON APIs. Idempotent requests are retried on transient failures;
writes are never retried automatically, because a timed-out write may have succeeded."""
from __future__ import annotations

import json
import time

import httpx

from ..safety import HIGH_RISK, READ, WRITE
from .base import Action, Tool, ToolContext, ToolError, ToolResult, schema

BODY_LIMIT = 10_000
IDEMPOTENT = {"GET", "HEAD", "OPTIONS"}


def http_request(ctx: ToolContext, args: dict) -> ToolResult:
    method = args.get("method", "GET").upper()
    url = args["url"]
    if not url.startswith("http"):
        url = ctx.env.start_url.rstrip("/") + "/" + url.lstrip("/")
    if err := ctx.policy.check_url(url):
        raise ToolError(err)
    attempts = 3 if method in IDEMPOTENT else 1
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            resp = httpx.request(method, url, headers=args.get("headers"), params=args.get("params"),
                                 json=args.get("json"), timeout=15)
            if resp.status_code >= 500 and attempt < attempts:
                time.sleep(0.5 * 2 ** attempt)
                continue
            break
        except httpx.TransportError as e:
            last_exc = e
            if attempt == attempts:
                hint = "" if method in IDEMPOTENT else " The request may or may not have reached the server; " \
                                                     "check the current state before retrying."
                raise ToolError(f"{method} {url} failed: {type(e).__name__}: {e}.{hint}") from e
            time.sleep(0.5 * 2 ** attempt)
    body = resp.text
    if "json" in resp.headers.get("content-type", ""):
        try:
            body = json.dumps(resp.json(), indent=1)
        except ValueError:
            pass
    if len(body) > BODY_LIMIT:
        body = body[:BODY_LIMIT] + f"\n[... truncated; {len(resp.text):,} chars total]"
    note = f" (after {attempt} attempts)" if attempt > 1 else ""
    return ToolResult(f"HTTP {resp.status_code} {resp.reason_phrase}{note}\n{body}", is_error=resp.status_code >= 400,
                      data={"http_status": resp.status_code})


def _describe(ctx: ToolContext, args: dict) -> Action:
    method = args.get("method", "GET").upper()
    url = args.get("url", "")
    effect = READ if method in IDEMPOTENT else HIGH_RISK if method == "DELETE" else WRITE
    return Action(effect, f"{method} {url}", url)


TOOLS = [
    Tool("http_request",
         "Call an HTTP/JSON API inside the sandbox. Use {{secret:NAME}} in headers for API keys. "
         "Returns status and body.",
         schema({
             "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"]},
             "url": {"type": "string"},
             "headers": {"type": "object"},
             "params": {"type": "object"},
             "json": {"type": "object", "description": "JSON request body"},
         }, ["method", "url"]), http_request, _describe),
]
