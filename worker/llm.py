"""Model access. The agent depends only on the small `LLM` protocol below, so the backend
(Gemini/Groq/Ollama via the OpenAI-compatible API, or Claude) can be swapped, and tests use a
scripted model."""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

import anthropic

# USD per million tokens, for the cost estimate in reports.
PRICES = {"claude-opus-5-5": (4.0, 20.0), "claude-sonnet-5-5": (2.0, 10.0), "claude-haiku-5-5": (0.10, 0.50),
          "claude-fable-5-1": (10.0, 50.0)}


@dataclass
class LLMResponse:
    content: list[dict]          # API-shaped content blocks; appended to history verbatim
    stop_reason: str
    usage: dict = field(default_factory=dict)
    model: str = ""


class LLM(Protocol):
    model: str

    def create(self, system: str, tools: list[dict], messages: list[dict], max_tokens: int = 16_000) -> LLMResponse:
        ...


class LLMError(RuntimeError):
    pass


class ClaudeLLM:
    """Claude via the Anthropic Messages API (beta surface for the optional features).

    - adaptive thinking with display "updates": the model's short progress notes between tool calls are
      returned as readable text (shown live in the UI) while raw reasoning stays hidden
    - server-side refusal fallbacks ("default" routing)
    - automatic prompt caching: the conversation prefix is reused across the agent loop's turns
    Optional features are dropped automatically if the API rejects them (e.g. with a different model).
    """

    OPTIONAL_BETAS = ["thinking-display-updates-2026-08-18", "server-side-fallback-2026-07-01"]

    def __init__(self, model: str = "claude-opus-5-5", effort: str = "high"):
        self.model = model
        self.effort = effort
        self.client = anthropic.Anthropic(max_retries=4, timeout=300)
        self.optional_features = True

    def _params(self) -> dict:
        params: dict = {"output_config": {"effort": self.effort}, "cache_control": {"type": "ephemeral"}}
        if self.optional_features:
            params.update(betas=self.OPTIONAL_BETAS, fallbacks="default",
                          thinking={"type": "adaptive", "display": "updates"})
        else:
            params["thinking"] = {"type": "adaptive"}
        return params

    def create(self, system: str, tools: list[dict], messages: list[dict], max_tokens: int = 16_000) -> LLMResponse:
        for attempt in (1, 2):
            try:
                resp = self.client.beta.messages.create(
                    model=self.model, max_tokens=max_tokens, system=system, tools=tools, messages=messages,
                    **self._params())
                break
            except anthropic.BadRequestError as e:
                if attempt == 1 and self.optional_features:
                    self.optional_features = False  # degrade gracefully, then retry once
                    continue
                raise LLMError(f"Claude API rejected the request: {e.message}") from e
            except anthropic.AuthenticationError as e:
                raise LLMError("No valid Anthropic credentials. Set ANTHROPIC_API_KEY.") from e
            except anthropic.NotFoundError as e:
                raise LLMError(f"Model '{self.model}' not found: {e.message}") from e
            except anthropic.RateLimitError as e:
                raise LLMError("Rate limited by the Claude API after retries; try again later.") from e
            except anthropic.APIStatusError as e:
                raise LLMError(f"Claude API error {e.status_code}: {e.message}") from e
            except anthropic.APIConnectionError as e:
                raise LLMError("Could not reach the Claude API (network error).") from e
        u = resp.usage
        usage = {"input_tokens": u.input_tokens, "output_tokens": u.output_tokens,
                 "cache_read_input_tokens": u.cache_read_input_tokens or 0,
                 "cache_creation_input_tokens": u.cache_creation_input_tokens or 0}
        return LLMResponse([b.to_dict() for b in resp.content], resp.stop_reason or "end_turn", usage, resp.model)


class OpenAICompatLLM:
    """Any OpenAI-compatible chat-completions backend: Gemini (free tier), Groq (free tier), Ollama (local).

    The agent keeps its history in Anthropic-style content blocks; this adapter translates to/from the
    chat-completions format on every call. Because these backends have no prompt caching and free tiers
    are token-limited, large observations older than the last few are elided in the *request* only (the
    agent's own history, trace and verifier input stay complete).
    """

    def __init__(self, provider: str, model: str, effort: str = "high"):
        import openai

        cfg = PROVIDERS[provider]
        api_key = next((os.environ[k] for k in cfg["key_env"] if os.environ.get(k)), None)
        if cfg["key_env"] and not api_key:
            raise LLMError(f"No API key for {provider}. Set {' or '.join(cfg['key_env'])} "
                           f"(free key: {cfg['key_url']}).")
        self.provider, self.model, self.cfg = provider, model, cfg
        self.effort = {"xhigh": "high", "max": "high"}.get(effort, effort)
        self.images = cfg["images"]
        self.min_interval = float(os.environ.get("WORKER_MIN_CALL_INTERVAL", cfg["min_interval"]))
        self.keep_full = int(os.environ.get("WORKER_KEEP_FULL_OBSERVATIONS", "6"))
        self._last_call = 0.0
        self._openai = openai
        self.client = openai.OpenAI(base_url=os.environ.get("WORKER_BASE_URL", cfg["base_url"]),
                                    api_key=api_key or "not-needed", max_retries=0, timeout=300)

    def create(self, system: str, tools: list[dict], messages: list[dict], max_tokens: int = 16_000) -> LLMResponse:
        openai = self._openai
        api_tools, json_args = to_openai_tools(tools)
        params: dict = {"model": self.model, "tools": api_tools, "max_tokens": max_tokens}
        if self.cfg.get("reasoning_effort") and self.effort in ("minimal", "low", "medium", "high"):
            params["reasoning_effort"] = self.effort
        delay = 5.0
        for attempt in range(1, 8):
            wait = self.min_interval - (time.time() - self._last_call)
            if wait > 0:
                time.sleep(wait)  # pace requests to stay under free-tier per-minute limits
            self._last_call = time.time()
            try:
                resp = self.client.chat.completions.create(
                    messages=to_openai_messages(system, messages, self.keep_full, self.images), **params)
                break
            except openai.RateLimitError as e:
                body = str(e)
                if "PerDay" in body or "per day" in body.lower():
                    raise LLMError(f"{self.provider} free-tier daily quota exhausted for {self.model}. Try again "
                                   f"tomorrow or set WORKER_MODEL to another free model.") from e
                if attempt == 7:
                    raise LLMError(f"{self.provider} kept rate-limiting requests (free tier).") from e
                retry_after = _retry_after_seconds(e)
                time.sleep(min(retry_after or delay, 65))
                delay = min(delay * 2, 60)
            except openai.BadRequestError as e:
                if self.images and "image" in str(e).lower():
                    self.images = False  # model can't take images: fall back to text-only observations
                    continue
                raise LLMError(f"{self.provider} rejected the request: {e}") from e
            except openai.AuthenticationError as e:
                raise LLMError(f"Invalid API key for {self.provider}.") from e
            except openai.NotFoundError as e:
                raise LLMError(f"Model '{self.model}' not found on {self.provider}: {e}") from e
            except openai.APIConnectionError as e:
                hint = " Is Ollama running (ollama serve)?" if self.provider == "ollama" else ""
                if attempt >= 3:
                    raise LLMError(f"Could not reach {self.provider}.{hint}") from e
                time.sleep(delay)
            except openai.APIStatusError as e:
                if e.status_code >= 500 and attempt < 4:
                    time.sleep(delay)
                    continue
                raise LLMError(f"{self.provider} API error {e.status_code}: {e}") from e
        return from_openai_response(resp, json_args)


def _retry_after_seconds(err) -> float | None:
    try:
        header = err.response.headers.get("retry-after")
        if header:
            return float(header)
    except (AttributeError, ValueError):
        pass
    m = re.search(r"retry(?:Delay| in)[\"': ]+(\d+(?:\.\d+)?)s", str(err))
    return float(m.group(1)) + 1 if m else None


def _sanitize_schema(node):
    """Removes JSON-schema keywords some OpenAI-compatible backends reject."""
    if isinstance(node, dict):
        return {k: _sanitize_schema(v) for k, v in node.items() if k != "additionalProperties"}
    if isinstance(node, list):
        return [_sanitize_schema(v) for v in node]
    return node


def to_openai_tools(tools: list[dict]) -> tuple[list[dict], dict[str, set[str]]]:
    """Converts tool definitions. Free-form object arguments (no declared properties) are exposed as
    JSON strings, which every backend accepts; returns which args to decode back per tool."""
    out, json_args = [], {}
    for t in tools:
        params = _sanitize_schema(t["input_schema"])
        for key, prop in params.get("properties", {}).items():
            if prop.get("type") == "object" and not prop.get("properties"):
                desc = (prop.get("description", "") + " (a JSON object, encoded as a string)").strip()
                params["properties"][key] = {"type": "string", "description": desc}
                json_args.setdefault(t["name"], set()).add(key)
        out.append({"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                     "parameters": params}})
    return out, json_args


ELIDE_MIN_CHARS = 1_500


def to_openai_messages(system: str, messages: list[dict], keep_full: int = 6, images: bool = True) -> list[dict]:
    results = [(mi, bi) for mi, m in enumerate(messages) if m["role"] == "user" and isinstance(m["content"], list)
               for bi, b in enumerate(m["content"]) if b.get("type") == "tool_result"]
    full = set(results[-keep_full:]) if keep_full > 0 else set()
    out: list[dict] = [{"role": "system", "content": system}]
    for mi, m in enumerate(messages):
        if m["role"] == "assistant":
            content = m["content"] if isinstance(m["content"], list) else [{"type": "text", "text": m["content"]}]
            msg: dict = {"role": "assistant",
                         "content": "\n".join(b["text"] for b in content if b.get("type") == "text") or None}
            calls = [b.get("provider_data") or {"id": b["id"], "type": "function", "function": {
                "name": b["name"], "arguments": json.dumps(b["input"])}}
                     for b in content if b.get("type") == "tool_use"]
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
            continue
        if isinstance(m["content"], str):
            out.append({"role": "user", "content": m["content"]})
            continue
        trailing: list[dict] = []
        for bi, b in enumerate(m["content"]):
            if b.get("type") == "tool_result":
                parts = b["content"] if isinstance(b["content"], list) else [{"type": "text", "text": b["content"]}]
                text_ = "\n".join(p.get("text", "") for p in parts if p.get("type") == "text") or "(no output)"
                if (mi, bi) not in full and len(text_) > ELIDE_MIN_CHARS:
                    head = "\n".join(text_.splitlines()[:3])
                    text_ = head + "\n[Older observation omitted to save context; re-read the page if needed.]"
                out.append({"role": "tool", "tool_call_id": b["tool_use_id"], "content": text_})
                imgs = [p for p in parts if p.get("type") == "image"]
                if imgs and (mi, bi) in full and images:
                    trailing.append({"type": "text", "text": "Image returned by the tool call above:"})
                    trailing += [{"type": "image_url", "image_url": {
                        "url": f"data:{p['source']['media_type']};base64,{p['source']['data']}"}} for p in imgs]
            elif b.get("type") == "text":
                trailing.append({"type": "text", "text": b["text"]})
        if trailing:
            out.append({"role": "user", "content": trailing})
    return out


def from_openai_response(resp, json_args: dict[str, set[str]]) -> LLMResponse:
    import uuid

    choice = resp.choices[0]
    msg = choice.message
    blocks: list[dict] = []
    if msg.content and msg.content.strip():
        blocks.append({"type": "text", "text": msg.content})
    for tc in msg.tool_calls or []:
        raw = tc.model_dump(exclude_none=True)  # keeps provider extras (e.g. Gemini thought signatures)
        raw["id"] = tc.id or "call_" + uuid.uuid4().hex[:12]
        try:
            args = json.loads(tc.function.arguments or "{}")
            if not isinstance(args, dict):
                args = {"__invalid_arguments__": tc.function.arguments}
        except json.JSONDecodeError:
            args = {"__invalid_json__": (tc.function.arguments or "")[:500]}
        for key in json_args.get(tc.function.name, ()):
            if isinstance(args.get(key), str):
                try:
                    args[key] = json.loads(args[key])
                except json.JSONDecodeError:
                    pass  # left as a string; tool input validation will report it to the model
        blocks.append({"type": "tool_use", "id": raw["id"], "name": tc.function.name, "input": args,
                       "provider_data": raw})
    if any(b["type"] == "tool_use" for b in blocks):
        stop = "tool_use"
    else:
        stop = {"length": "max_tokens", "content_filter": "refusal"}.get(choice.finish_reason, "end_turn")
    u = resp.usage
    usage = {"input_tokens": getattr(u, "prompt_tokens", 0) or 0,
             "output_tokens": getattr(u, "completion_tokens", 0) or 0}
    return LLMResponse(blocks or [{"type": "text", "text": ""}], stop, usage, resp.model or "")


PROVIDERS: dict[str, dict] = {
    "gemini": {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
               "key_env": ["GEMINI_API_KEY", "GOOGLE_API_KEY"], "key_url": "https://aistudio.google.com/apikey",
               "model": "gemini-3.8-flash", "images": True, "reasoning_effort": True, "min_interval": 4.0},
    "groq": {"base_url": "https://api.groq.com/openai/v1", "key_env": ["GROQ_API_KEY"],
             "key_url": "https://console.groq.com/keys", "model": "llama-3.3-70b-versatile", "images": False,
             "reasoning_effort": False, "min_interval": 2.0},
    "ollama": {"base_url": "http://localhost:11434/v1", "key_env": [], "key_url": "", "model": "qwen2.5:7b",
               "images": False, "reasoning_effort": False, "min_interval": 0.0},
    "anthropic": {"key_env": ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"], "key_url": "https://console.anthropic.com",
                  "model": "claude-opus-5-5"},
}


def make_llm(settings) -> "LLM":
    if settings.provider == "anthropic":
        return ClaudeLLM(settings.model, settings.effort)
    return OpenAICompatLLM(settings.provider, settings.model, settings.effort)


def has_credentials(provider: str) -> bool:
    keys = PROVIDERS[provider]["key_env"]
    return not keys or any(os.environ.get(k) for k in keys)


def estimate_cost(model: str, usage: dict) -> float | None:
    if model not in PRICES:
        return None
    pin, pout = PRICES[model]
    cached = usage.get("cache_read_input_tokens", 0)
    written = usage.get("cache_creation_input_tokens", 0)
    return round((usage.get("input_tokens", 0) * pin + written * pin * 1.25 + cached * pin * 0.1
                  + usage.get("output_tokens", 0) * pout) / 1e6, 4)


# --------------------------------------------------------------------------- testing

def tool_use(name: str, **args) -> dict:
    import uuid
    return {"type": "tool_use", "id": "toolu_" + uuid.uuid4().hex[:12], "name": name, "input": args}


def text(t: str) -> dict:
    return {"type": "text", "text": t}


class ScriptedLLM:
    """Plays back a script. Each step is a function(messages) -> list of content blocks,
    so scripts can react to observations (e.g. find an element ref in the latest snapshot)."""

    def __init__(self, steps: list[Callable[[list[dict]], list[dict]]], model: str = "scripted"):
        self.steps = list(steps)
        self.model = model
        self.calls = 0

    def create(self, system: str, tools: list[dict], messages: list[dict], max_tokens: int = 16_000) -> LLMResponse:
        self.calls += 1
        if not self.steps:
            return LLMResponse([text("(script exhausted)")], "end_turn")
        blocks = self.steps.pop(0)(messages)
        stop = "tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end_turn"
        return LLMResponse(blocks, stop, {"input_tokens": 0, "output_tokens": 0})


def last_tool_text(messages: list[dict]) -> str:
    """Concatenated text of the tool results in the most recent user message (for scripts)."""
    for msg in reversed(messages):
        if msg["role"] == "user" and isinstance(msg["content"], list):
            parts = []
            for block in msg["content"]:
                if block.get("type") == "tool_result":
                    parts += [c.get("text", "") for c in block["content"] if c.get("type") == "text"]
            if parts:
                return "\n".join(parts)
    return ""
