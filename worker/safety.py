"""Guardrails enforced by the harness, independent of what the model decides.

- SecretVault: the model references credentials as {{secret:NAME}}; real values
  are substituted only at execution time and scrubbed from every observation.
- ActionPolicy: network allowlist, and the rule for which actions need a human.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from .config import Environment

SECRET_RE = re.compile(r"\{\{\s*secret:([a-zA-Z0-9_]+)\s*\}\}")
REDACTED = "[REDACTED]"


class SecretError(ValueError):
    pass


class SecretVault:
    def __init__(self, env: Environment):
        self._secrets = {name: s.value for name, s in env.secrets.items() if s.value}
        self.descriptions = {name: s.description for name, s in env.secrets.items()}

    def substitute(self, value: Any) -> Any:
        """Deep-replace {{secret:NAME}} placeholders in tool arguments."""
        if isinstance(value, str):
            def repl(m: re.Match) -> str:
                name = m.group(1)
                if name not in self._secrets:
                    raise SecretError(f"Unknown secret '{name}'. Available: {', '.join(self._secrets) or 'none'}")
                return self._secrets[name]
            return SECRET_RE.sub(repl, value)
        if isinstance(value, dict):
            return {k: self.substitute(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.substitute(v) for v in value]
        return value

    def redact(self, text: str) -> str:
        for secret in sorted(self._secrets.values(), key=len, reverse=True):
            if len(secret) >= 4:
                text = text.replace(secret, REDACTED)
        return text


# Effect levels, from harmless to dangerous.
READ, LOCAL, WRITE, HIGH_RISK, CONTROL = "read", "local", "write", "high_risk", "control"


class ActionPolicy:
    def __init__(self, env: Environment, autonomy: str = "autonomous"):
        if autonomy not in ("autonomous", "supervised"):
            raise ValueError("autonomy must be 'autonomous' or 'supervised'")
        self.env = env
        self.autonomy = autonomy

    def check_url(self, url: str) -> str | None:
        """Returns an error message if the URL is outside the sandbox, else None."""
        p = urlparse(url)
        if p.scheme in ("about", "data", "blob"):
            return None
        origin = f"{p.scheme}://{p.netloc}".rstrip("/")
        if origin not in self.env.allowed_origins:
            return (f"Blocked by policy: {origin} is not an allowed origin. "
                    f"Allowed: {', '.join(self.env.allowed_origins)}")
        for prefix in self.env.blocked_paths:
            if p.path == prefix or p.path.startswith(prefix.rstrip("/") + "/"):
                return f"Blocked by policy: path {prefix} is off-limits to the agent."
        return None

    def escalate(self, effect: str, description: str) -> str:
        """Upgrades WRITE to HIGH_RISK when the action matches a high-risk pattern."""
        if effect == WRITE and any(p in description.lower() for p in self.env.high_risk_patterns):
            return HIGH_RISK
        return effect

    def needs_approval(self, effect: str) -> bool:
        if effect == HIGH_RISK:
            return True
        return effect == WRITE and self.autonomy == "supervised"
