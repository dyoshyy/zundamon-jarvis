"""One short English line per tool use, for the conversation watch window.

Only a few safe details are shown: a file's basename, a host, a search query, an agent or
command description, an event title. Never whole inputs (file contents, mail bodies,
commands, recipients).
"""

import re
from pathlib import PurePath
from typing import Any
from urllib.parse import urlsplit

DETAIL_MAX = 24

_FILE_TOOLS = {
    "Read": ("📖", "Read file"),
    "Write": ("📝", "Save note"),
    "Edit": ("✏", "Edit file"),
    "MultiEdit": ("✏", "Edit file"),
    "NotebookEdit": ("✏", "Edit file"),
}

_PLAIN = {
    "Glob": "🔍 Find files",
    "Grep": "🔍 Search in files",
    "ToolSearch": "🧰 Load tools",
    "TodoWrite": "🗒 Update todos",
    "TaskOutput": "🧵 Check background task",
    "BashOutput": "⌨ Check command output",
    "KillShell": "⏹ Stop command",
    "TaskStop": "⏹ Stop background task",
}

# (server substring, emoji, display name, [(tool-name pattern, label, detail key)])
_MCP: list[tuple[str, str, str, list[tuple[str, str, str | None]]]] = [
    ("Calendar", "📅", "Calendar", [
        (r"^(list|search|get|suggest)", "Check calendar", None),
        (r"^create", "Create event", "summary"),
        (r"^update", "Update event", "summary"),
        (r"^(delete|cancel)", "Delete event", None),
        (r"^respond", "Reply to event", None),
    ]),
    ("Gmail", "✉", "Gmail", [
        (r"^(search|list)", "Search mail", None),
        (r"^get", "Read mail", None),
        (r"^(send|reply|forward)", "Send mail", None),
        (r"draft", "Create draft", None),
        (r"^(trash|delete)", "Delete mail", None),
    ]),
    ("Notion", "🗂", "Notion", [
        (r"search", "Search Notion", None),
        (r"fetch", "Read Notion", None),
        (r"create-pages", "Create Notion page", None),
        (r"update", "Update Notion", None),
    ]),
    ("Drive", "📁", "Drive", [
        (r"^(search|list)", "Search Drive", None),
        (r"^(read|download|get)", "Read Drive file", None),
        (r"^(create|update|copy)", "Save to Drive", None),
    ]),
]


def _short(value: Any, limit: int = DETAIL_MAX) -> str:
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _with(head: str, detail: str) -> str:
    return f"{head}: {detail}" if detail else head


def _basename(value: Any) -> str:
    return _short(PurePath(value).name, 40) if isinstance(value, str) and value else ""


def _host(value: Any) -> str:
    try:
        return urlsplit(value).hostname or "" if isinstance(value, str) else ""
    except ValueError:
        return ""


def _mcp_label(name: str, inp: dict) -> str:
    _, server, tool = name.split("__", 2)
    for key, emoji, display, rules in _MCP:
        if key in server:
            for pattern, label, detail in rules:
                if re.search(pattern, tool):
                    return _with(f"{emoji} {label}", _short(inp.get(detail)) if detail else "")
            return f"{emoji} {display}: {tool}"
    short_server = server.removeprefix("claude_ai_")
    return f"🔧 {short_server}: {tool}"


def tool_label(name: str, input: Any) -> str:
    inp = input if isinstance(input, dict) else {}
    if name in _FILE_TOOLS:
        emoji, label = _FILE_TOOLS[name]
        return _with(f"{emoji} {label}", _basename(inp.get("file_path") or inp.get("notebook_path")))
    if name in _PLAIN:
        return _PLAIN[name]
    if name == "WebSearch":
        return _with("🔎 Web search", _short(inp.get("query")))
    if name == "WebFetch":
        return _with("🌐 Read page", _host(inp.get("url")))
    if name in ("Agent", "Task"):
        head = "🧵 Start background task" if inp.get("run_in_background") else "🧵 Ask subagent"
        return _with(head, _short(inp.get("description")))
    if name == "Bash":
        return _with("⌨ Run command", _short(inp.get("description")))
    if name == "Skill":
        return _with("🧩 Use skill", _short(inp.get("skill")))
    if name.startswith("mcp__") and name.count("__") >= 2:
        return _mcp_label(name, inp)
    return f"🔧 {name}"
