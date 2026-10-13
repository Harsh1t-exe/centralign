"""Runtime configuration: the environment profile plus agent settings."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_FILE = ROOT / "environments" / "acme.toml"


@dataclass
class Secret:
    name: str
    value: str
    description: str = ""


@dataclass
class Environment:
    name: str
    start_url: str
    description: str
    allowed_origins: list[str]
    blocked_paths: list[str] = field(default_factory=list)
    high_risk_patterns: list[str] = field(default_factory=list)
    user: dict = field(default_factory=dict)
    secrets: dict[str, Secret] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path = DEFAULT_ENV_FILE) -> "Environment":
        raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))
        secrets = {}
        for name, spec in raw.get("secrets", {}).items():
            spec = spec if isinstance(spec, dict) else {"value": spec}
            value = os.environ.get(f"WORKER_SECRET_{name.upper()}", spec.get("value", ""))
            secrets[name] = Secret(name, value, spec.get("description", ""))
        return cls(
            name=raw["name"],
            start_url=raw["start_url"],
            description=raw.get("description", "").strip(),
            allowed_origins=[o.rstrip("/") for o in raw.get("allowed_origins", [])],
            blocked_paths=raw.get("blocked_paths", []),
            high_risk_patterns=[p.lower() for p in raw.get("high_risk_patterns", [])],
            user=raw.get("user", {}),
            secrets=secrets,
        )


@dataclass
class Settings:
    # LLM backend: "gemini" (free tier, default), "groq" (free tier), "ollama" (local), "anthropic" (paid).
    provider: str = os.environ.get("WORKER_PROVIDER", "gemini").lower()
    model: str = os.environ.get("WORKER_MODEL", "")  # empty -> the provider's default model
    effort: str = os.environ.get("WORKER_EFFORT", "high")
    max_steps: int = int(os.environ.get("WORKER_MAX_STEPS", "60"))
    max_verification_rounds: int = 2
    # "autonomous": the agent acts on its own; only high-risk actions need a human.
    # "supervised": every external write (form submit, POST...) needs a human.
    autonomy: str = os.environ.get("WORKER_AUTONOMY", "autonomous")
    verify: bool = os.environ.get("WORKER_VERIFY", "1") != "0"
    headless: bool = os.environ.get("WORKER_HEADED", "0") != "1"
    runs_dir: Path = Path(os.environ.get("WORKER_RUNS_DIR", ROOT / "runs"))

    def __post_init__(self) -> None:
        from .llm import PROVIDERS
        if self.provider not in PROVIDERS:
            raise ValueError(f"Unknown WORKER_PROVIDER '{self.provider}'. Choose from: {', '.join(PROVIDERS)}")
        if not self.model:
            self.model = PROVIDERS[self.provider]["model"]
