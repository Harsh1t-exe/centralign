"""Workspace file tools. All paths are confined to the run's workspace folder."""
from __future__ import annotations

from pathlib import Path

from pypdf import PdfReader

from ..safety import LOCAL
from .base import Action, Tool, ToolContext, ToolError, ToolResult, schema

TEXT_LIMIT = 20_000
TEXT_SUFFIXES = {".txt", ".md", ".csv", ".json", ".html", ".xml", ".log", ".tsv", ".yaml", ".yml"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


def _path(ctx: ToolContext, rel: str) -> Path:
    root = ctx.run.workspace.resolve()
    p = (root / rel).resolve()
    if p != root and root not in p.parents:
        raise ToolError("Path escapes the workspace; use paths relative to the workspace.")
    return p


def list_files(ctx: ToolContext, args: dict) -> ToolResult:
    root = ctx.run.workspace
    files = [f"{p.relative_to(root).as_posix()} ({p.stat().st_size:,} bytes)"
             for p in sorted(root.rglob("*")) if p.is_file()]
    return ToolResult("Workspace files:\n" + "\n".join(files) if files else "The workspace is empty.")


def read_file(ctx: ToolContext, args: dict) -> ToolResult:
    p = _path(ctx, args["path"])
    if not p.is_file():
        raise ToolError(f"No such file: {args['path']}. Use list_files to see the workspace.")
    suffix = p.suffix.lower()
    if suffix == ".pdf":
        reader = PdfReader(str(p))
        pages = [f"--- page {i} ---\n{page.extract_text() or ''}" for i, page in enumerate(reader.pages, 1)]
        text = "\n".join(pages)
        if not text.strip().replace("-", "").replace("page", "").strip():
            return ToolResult(f"{args['path']}: PDF has no extractable text (probably a scan).", is_error=True)
        header = f"{args['path']} (PDF, {len(reader.pages)} page(s)):\n"
    elif suffix in IMAGE_SUFFIXES:
        return ToolResult(f"Image {args['path']}:", images=[str(p)])
    elif suffix in TEXT_SUFFIXES or p.stat().st_size < 200_000:
        text = p.read_text(encoding="utf-8", errors="replace")
        header = f"{args['path']}:\n"
    else:
        raise ToolError(f"Unsupported file type: {suffix}")
    if len(text) > TEXT_LIMIT:
        text = text[:TEXT_LIMIT] + f"\n[... truncated; file has {len(text):,} chars]"
    return ToolResult(header + text)


def write_file(ctx: ToolContext, args: dict) -> ToolResult:
    p = _path(ctx, args["path"])
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(args["content"], encoding="utf-8")
    rel = p.relative_to(ctx.run.workspace.resolve()).as_posix()
    ctx.run.emit("file_written", path=rel, bytes=p.stat().st_size)
    return ToolResult(f"Wrote {len(args['content']):,} chars to {rel}.")


TOOLS = [
    Tool("list_files", "List files in the run workspace (downloads and files you wrote).", schema({}), list_files),
    Tool("read_file", "Read a workspace file: PDFs are converted to text, images are shown to you, text is returned.",
         schema({"path": {"type": "string", "description": "Path relative to the workspace, e.g. downloads/x.pdf"}},
                ["path"]), read_file),
    Tool("write_file", "Create or overwrite a text file in the workspace (e.g. a CSV or report the user asked for).",
         schema({"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]), write_file,
         lambda ctx, a: Action(LOCAL, f"write workspace file {a.get('path')}")),
]
