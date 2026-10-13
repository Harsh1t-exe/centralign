"""Tool abstraction shared by every capability (browser, files, HTTP, control)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from ..safety import READ

if TYPE_CHECKING:
    from ..config import Environment, Settings
    from ..events import Run
    from ..interaction import InteractionBroker
    from ..safety import ActionPolicy, SecretVault
    from .browser import BrowserSession


class ToolError(Exception):
    """An expected failure, reported to the model as an error observation (not a crash)."""


@dataclass
class ToolResult:
    content: str
    is_error: bool = False
    images: list[str] = field(default_factory=list)  # PNG paths attached to the model as image blocks
    data: dict = field(default_factory=dict)         # structured extras for the trace / UI


@dataclass
class Action:
    """What a tool call is about to do, for policy decisions and the audit log."""
    effect: str = READ
    description: str = ""
    url: str | None = None


@dataclass
class Tool:
    name: str
    description: str
    schema: dict
    handler: Callable[["ToolContext", dict], ToolResult]
    # Describes the side effect of a specific call (args already secret-substituted).
    describe: Callable[["ToolContext", dict], Action] = lambda ctx, args: Action()

    def to_api(self) -> dict:
        return {"name": self.name, "description": self.description, "input_schema": self.schema}


@dataclass
class ToolContext:
    run: "Run"
    env: "Environment"
    settings: "Settings"
    policy: "ActionPolicy"
    vault: "SecretVault"
    broker: "InteractionBroker"
    browser: "BrowserSession | None" = None
    finished: dict | None = None        # set by the finish tool
    approval_tokens: int = 0            # granted by request_approval, consumed by the next gated action


def schema(properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": properties, "required": required or [],
            "additionalProperties": False}


_TYPES: dict[str, Any] = {"string": str, "integer": int, "number": (int, float), "boolean": bool,
                          "array": list, "object": dict}


def validate_input(tool: Tool, args: Any) -> str | None:
    """Light JSON-schema validation; returns an error message for the model, or None."""
    if not isinstance(args, dict):
        return "Tool input must be a JSON object."
    props = tool.schema.get("properties", {})
    missing = [k for k in tool.schema.get("required", []) if k not in args]
    if missing:
        return f"Missing required argument(s): {', '.join(missing)}."
    unknown = [k for k in args if k not in props]
    if unknown:
        return f"Unknown argument(s): {', '.join(unknown)}. Allowed: {', '.join(props)}."
    for key, value in args.items():
        expected = props[key].get("type")
        if expected and not isinstance(value, _TYPES[expected]) or (expected == "integer" and isinstance(value, bool)):
            return f"Argument '{key}' must be of type {expected}."
        if "enum" in props[key] and value not in props[key]["enum"]:
            return f"Argument '{key}' must be one of {props[key]['enum']}."
    return None
