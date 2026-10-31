"""One short line per tool use, for the conversation watch window: ``tool_label(name, input,
lang)``, English by default (the watch event's ``label``; ``labels`` has every language).

Only a few safe details are shown: a file's basename, a host, a search query, an agent or
command description, an event title. Never whole inputs (file contents, mail bodies,
commands, recipients).
"""

import re
from pathlib import PurePath
from typing import Any
from urllib.parse import urlsplit

from zundamon_jarvis import i18n

DETAIL_MAX = 24

_FILE_TOOLS = {
    "Read": ("📖", "Read file"),
    "Write": ("📝", "Save note"),
    "Edit": ("✏", "Edit file"),
    "MultiEdit": ("✏", "Edit file"),
    "NotebookEdit": ("✏", "Edit file"),
}

_PLAIN = {
    "Glob": ("🔍", "Find files"),
    "Grep": ("🔍", "Search in files"),
    "ToolSearch": ("🧰", "Load tools"),
    "TodoWrite": ("🗒", "Update todos"),
    "TaskOutput": ("🧵", "Check background task"),
    "BashOutput": ("⌨", "Check command output"),
    "KillShell": ("⏹", "Stop command"),
    "TaskStop": ("⏹", "Stop background task"),
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


def _mcp_label(name: str, inp: dict, lang: str) -> str:
    _, server, tool = name.split("__", 2)
    for key, emoji, display, rules in _MCP:
        if key in server:
            for pattern, label, detail in rules:
                if re.search(pattern, tool):
                    return _with(f"{emoji} {_t(label, lang)}", _short(inp.get(detail)) if detail else "")
            return f"{emoji} {_t(display, lang)}: {tool}"
    short_server = server.removeprefix("claude_ai_")
    return f"🔧 {short_server}: {tool}"


def tool_label(name: str, input: Any, lang: str = "en") -> str:
    inp = input if isinstance(input, dict) else {}
    if name in _FILE_TOOLS:
        emoji, label = _FILE_TOOLS[name]
        return _with(f"{emoji} {_t(label, lang)}", _basename(inp.get("file_path") or inp.get("notebook_path")))
    if name in _PLAIN:
        emoji, label = _PLAIN[name]
        return f"{emoji} {_t(label, lang)}"
    if name == "WebSearch":
        return _with(f"🔎 {_t('Web search', lang)}", _short(inp.get("query")))
    if name == "WebFetch":
        return _with(f"🌐 {_t('Read page', lang)}", _host(inp.get("url")))
    if name in ("Agent", "Task"):
        head = "Start background task" if inp.get("run_in_background") else "Ask subagent"
        return _with(f"🧵 {_t(head, lang)}", _short(inp.get("description")))
    if name == "Bash":
        return _with(f"⌨ {_t('Run command', lang)}", _short(inp.get("description")))
    if name == "Skill":
        return _with(f"🧩 {_t('Use skill', lang)}", _short(inp.get("skill")))
    if name.startswith("mcp__") and name.count("__") >= 2:
        return _mcp_label(name, inp, lang)
    return f"🔧 {name}"


# The labels above, by their English text.
TEXT = {
    "Read file": {"ja": "ファイルを読む", "en": "Read file"},
    "Save note": {"ja": "メモを保存", "en": "Save note"},
    "Edit file": {"ja": "ファイルを編集", "en": "Edit file"},
    "Find files": {"ja": "ファイルを探す", "en": "Find files"},
    "Search in files": {"ja": "ファイル内を検索", "en": "Search in files"},
    "Load tools": {"ja": "ツールを読み込む", "en": "Load tools"},
    "Update todos": {"ja": "TODO を更新", "en": "Update todos"},
    "Check background task": {"ja": "バックグラウンドの作業を確認", "en": "Check background task"},
    "Check command output": {"ja": "コマンドの出力を確認", "en": "Check command output"},
    "Stop command": {"ja": "コマンドを止める", "en": "Stop command"},
    "Stop background task": {"ja": "バックグラウンドの作業を止める", "en": "Stop background task"},
    "Web search": {"ja": "Web 検索", "en": "Web search"},
    "Read page": {"ja": "ページを読む", "en": "Read page"},
    "Start background task": {"ja": "バックグラウンドで作業開始", "en": "Start background task"},
    "Ask subagent": {"ja": "サブエージェントに依頼", "en": "Ask subagent"},
    "Run command": {"ja": "コマンドを実行", "en": "Run command"},
    "Use skill": {"ja": "スキルを使う", "en": "Use skill"},
    "Calendar": {"ja": "カレンダー", "en": "Calendar"},
    "Check calendar": {"ja": "予定を確認", "en": "Check calendar"},
    "Create event": {"ja": "予定を作成", "en": "Create event"},
    "Update event": {"ja": "予定を更新", "en": "Update event"},
    "Delete event": {"ja": "予定を削除", "en": "Delete event"},
    "Reply to event": {"ja": "予定に返事", "en": "Reply to event"},
    "Gmail": {"ja": "Gmail", "en": "Gmail"},
    "Search mail": {"ja": "メールを検索", "en": "Search mail"},
    "Read mail": {"ja": "メールを読む", "en": "Read mail"},
    "Send mail": {"ja": "メールを送信", "en": "Send mail"},
    "Create draft": {"ja": "下書きを作成", "en": "Create draft"},
    "Delete mail": {"ja": "メールを削除", "en": "Delete mail"},
    "Notion": {"ja": "Notion", "en": "Notion"},
    "Search Notion": {"ja": "Notion を検索", "en": "Search Notion"},
    "Read Notion": {"ja": "Notion を読む", "en": "Read Notion"},
    "Create Notion page": {"ja": "Notion のページを作成", "en": "Create Notion page"},
    "Update Notion": {"ja": "Notion を更新", "en": "Update Notion"},
    "Drive": {"ja": "Drive", "en": "Drive"},
    "Search Drive": {"ja": "Drive を検索", "en": "Search Drive"},
    "Read Drive file": {"ja": "Drive のファイルを読む", "en": "Read Drive file"},
    "Save to Drive": {"ja": "Drive に保存", "en": "Save to Drive"},
}


def _t(english: str, lang: str) -> str:
    return i18n.text(TEXT, english, lang) if english in TEXT else english
