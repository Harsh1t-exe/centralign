from . import browser, control, files, http
from .base import Tool

ALL_TOOLS: list[Tool] = [*control.TOOLS, *browser.TOOLS, *files.TOOLS, *http.TOOLS]

# Tools the independent verifier may use: observation only, no side effects.
READ_ONLY_TOOL_NAMES = {"browser_navigate", "browser_snapshot", "browser_screenshot", "browser_back",
                        "browser_click", "browser_type", "browser_select", "read_file", "list_files", "http_request"}
