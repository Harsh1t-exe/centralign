"""Run state and the append-only event log.

Every observable thing the worker does becomes an event. Events are written to
runs/<id>/trace.jsonl (the audit trail / debugging record) and streamed to the
web UI. The run object also holds the agent's working memory and plan.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


@dataclass
class Run:
    task: str
    runs_dir: Path
    id: str = field(default_factory=lambda: time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6])
    status: str = "running"
    events: list[dict] = field(default_factory=list)
    plan: list[dict] = field(default_factory=list)
    memory: dict[str, dict] = field(default_factory=dict)
    actions: list[dict] = field(default_factory=list)       # external side effects performed
    interactions: list[dict] = field(default_factory=list)  # questions / approvals and answers
    screenshots: list[str] = field(default_factory=list)
    report: dict | None = None
    usage: dict = field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
                                                 "cache_creation_input_tokens": 0, "llm_calls": 0})
    started_at: float = field(default_factory=time.time)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _listeners: list[Callable[[dict], None]] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.workspace.mkdir(exist_ok=True)
        (self.dir / "screens").mkdir(exist_ok=True)

    @property
    def dir(self) -> Path:
        return self.runs_dir / self.id

    @property
    def workspace(self) -> Path:
        return self.dir / "files"

    def emit(self, type_: str, **payload: Any) -> dict:
        with self._lock:
            event = {"seq": len(self.events), "ts": round(time.time() - self.started_at, 2), "type": type_, **payload}
            self.events.append(event)
            with open(self.dir / "trace.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(event, default=str) + "\n")
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception:  # a broken UI listener must never break the run
                pass
        return event

    def subscribe(self, fn: Callable[[dict], None]) -> None:
        self._listeners.append(fn)

    def next_screenshot_path(self) -> Path:
        return self.dir / "screens" / f"{len(self.screenshots) + 1:03d}.png"
