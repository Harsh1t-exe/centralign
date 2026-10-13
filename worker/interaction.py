"""Human-in-the-loop channel: clarifying questions and approvals.

The agent loop blocks on `ask()`; a front-end (terminal or web UI) answers it.
"""
from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from typing import Callable

from .events import Run


@dataclass
class Interaction:
    kind: str                      # "question" | "approval"
    prompt: str
    options: list[str] = field(default_factory=list)
    details: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    answer: str | None = None
    approved: bool | None = None
    _done: threading.Event = field(default_factory=threading.Event, repr=False)


class InteractionBroker:
    """Base broker: publishes the request as an event and waits for `resolve()`."""

    def __init__(self, run: Run, timeout_s: float | None = None):
        self.run = run
        self.timeout_s = timeout_s
        self.pending: dict[str, Interaction] = {}

    def ask(self, kind: str, prompt: str, options: list[str] | None = None, details: str = "") -> Interaction:
        item = Interaction(kind=kind, prompt=prompt, options=options or [], details=details)
        self.pending[item.id] = item
        self.run.emit("interaction_requested", id=item.id, kind=kind, prompt=prompt, options=item.options,
                      details=details)
        self._present(item)
        if not item._done.wait(self.timeout_s):
            item.answer, item.approved = "No response from the user (timed out).", False
        self.pending.pop(item.id, None)
        record = {"id": item.id, "kind": kind, "prompt": prompt, "details": details, "answer": item.answer,
                  "approved": item.approved}
        self.run.interactions.append(record)
        self.run.emit("interaction_resolved", **record)
        return item

    def resolve(self, interaction_id: str, answer: str, approved: bool | None = None) -> bool:
        item = self.pending.get(interaction_id)
        if not item:
            return False
        item.answer, item.approved = answer, approved
        item._done.set()
        return True

    def _present(self, item: Interaction) -> None:
        """Hook for front-ends that need to actively prompt (e.g. the terminal)."""


class TerminalBroker(InteractionBroker):
    def __init__(self, run: Run, input_fn: Callable[[str], str] = input):
        super().__init__(run)
        self.input_fn = input_fn

    def _present(self, item: Interaction) -> None:
        print("\n" + "=" * 70)
        if item.kind == "approval":
            print(f"APPROVAL NEEDED: {item.prompt}")
            if item.details:
                print(item.details)
            ans = self.input_fn("Approve? [y/N] (optionally add a comment after the y/n): ").strip()
            approved = ans.lower().startswith("y")
            comment = ans[1:].strip(" ,:-") if ans[:1].lower() in "yn" else ans
            self.resolve(item.id, comment or ("approved" if approved else "denied"), approved)
        else:
            print(f"QUESTION: {item.prompt}")
            if item.details:
                print(item.details)
            for i, opt in enumerate(item.options, 1):
                print(f"  {i}. {opt}")
            ans = self.input_fn("Your answer: ").strip()
            if ans.isdigit() and 1 <= int(ans) <= len(item.options):
                ans = item.options[int(ans) - 1]
            self.resolve(item.id, ans)
        print("=" * 70 + "\n")


class AutoBroker(InteractionBroker):
    """Scripted answers, for tests and unattended evaluation runs."""

    def __init__(self, run: Run, answers: Callable[[Interaction], tuple[str, bool | None]]):
        super().__init__(run)
        self.answers = answers

    def _present(self, item: Interaction) -> None:
        answer, approved = self.answers(item)
        self.resolve(item.id, answer, approved)
