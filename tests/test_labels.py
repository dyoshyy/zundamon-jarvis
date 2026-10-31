import pytest

from zundamon_jarvis.labels import tool_label

CAL = "mcp__claude_ai_Google_Calendar__"
GMAIL = "mcp__claude_ai_Gmail__"
NOTION = "mcp__claude_ai_Notion__"
DRIVE = "mcp__claude_ai_Google_Drive__"


@pytest.mark.parametrize("name, inp, label", [
    ("Read", {"file_path": "/home/alice/notes/x.md"}, "📖 Read file: x.md"),
    ("Write", {"file_path": "/a/b/memo.md", "content": "secret body"}, "📝 Save note: memo.md"),
    ("Edit", {"file_path": "/a/b/note.md", "old_string": "a", "new_string": "b"}, "✏ Edit file: note.md"),
    ("MultiEdit", {"file_path": "/a/n.md", "edits": []}, "✏ Edit file: n.md"),
    ("NotebookEdit", {"notebook_path": "/a/nb.ipynb"}, "✏ Edit file: nb.ipynb"),
    ("Glob", {"pattern": "**/*.md"}, "🔍 Find files"),
    ("Grep", {"pattern": "password"}, "🔍 Search in files"),
    ("WebSearch", {"query": "東京 天気"}, "🔎 Web search: 東京 天気"),
    ("WebFetch", {"url": "https://example.com/a/b?q=1", "prompt": "x"}, "🌐 Read page: example.com"),
    ("Agent", {"description": "Count notes", "prompt": "long", "run_in_background": True},
     "🧵 Start background task: Count notes"),
    ("Task", {"description": "Count notes", "prompt": "long"}, "🧵 Ask subagent: Count notes"),
    ("Bash", {"command": "rm -rf / --no-preserve-root"}, "⌨ Run command"),
    ("Bash", {"command": "git log", "description": "Show history"}, "⌨ Run command: Show history"),
    ("Skill", {"skill": "weekend-blocking"}, "🧩 Use skill: weekend-blocking"),
    ("ToolSearch", {"query": "select:x"}, "🧰 Load tools"),
    ("TodoWrite", {"todos": []}, "🗒 Update todos"),
    (CAL + "list_events", {"timeMin": "x"}, "📅 Check calendar"),
    (CAL + "list_calendars", {}, "📅 Check calendar"),
    (CAL + "search_events", {"query": "ジム"}, "📅 Check calendar"),
    (CAL + "create_event", {"summary": "ジム", "startTime": "x"}, "📅 Create event: ジム"),
    (CAL + "update_event", {"eventId": "e1"}, "📅 Update event"),
    (CAL + "delete_event", {"eventId": "e1"}, "📅 Delete event"),
    (GMAIL + "search_threads", {"query": "from:boss"}, "✉ Search mail"),
    (GMAIL + "get_message", {"messageId": "m"}, "✉ Read mail"),
    (GMAIL + "get_thread", {"threadId": "t"}, "✉ Read mail"),
    (GMAIL + "send_message", {"to": "a@b.c", "body": "secret"}, "✉ Send mail"),
    (GMAIL + "create_draft", {"to": "a@b.c"}, "✉ Create draft"),
    (NOTION + "notion-search", {"query": "x"}, "🗂 Search Notion"),
    (NOTION + "notion-fetch", {"id": "x"}, "🗂 Read Notion"),
    (NOTION + "notion-create-pages", {}, "🗂 Create Notion page"),
    (NOTION + "notion-update-page", {}, "🗂 Update Notion"),
    (DRIVE + "search_files", {"query": "x"}, "📁 Search Drive"),
    (DRIVE + "read_file_content", {"fileId": "x"}, "📁 Read Drive file"),
])
def test_known_tools(name, inp, label):
    assert tool_label(name, inp) == label


def test_unknown_tool_falls_back_to_its_name():
    assert tool_label("FancyNewTool", {"secret": "x"}) == "🔧 FancyNewTool"


def test_unknown_mcp_tool_shows_server_and_tool_only():
    assert tool_label("mcp__claude_ai_freee__freee_api_get", {"path": "/x"}) == "🔧 freee: freee_api_get"
    assert tool_label(GMAIL + "label_thread", {}) == "✉ Gmail: label_thread"


def test_long_details_are_cut_short():
    label = tool_label("WebSearch", {"query": "あ" * 100})
    assert label.startswith("🔎 Web search: ") and label.endswith("…") and len(label) < 40


def test_whitespace_in_details_is_collapsed():
    assert tool_label("Agent", {"description": "two\nlines  here"}) == "🧵 Ask subagent: two lines here"


@pytest.mark.parametrize("inp", [None, {}, {"file_path": None}, {"file_path": 3}, "garbage"])
def test_odd_input_never_raises(inp):
    assert tool_label("Read", inp) == "📖 Read file"


def test_calendar_title_is_optional():
    assert tool_label(CAL + "create_event", {}) == "📅 Create event"


@pytest.mark.parametrize("name, inp, label", [
    ("Read", {"file_path": "/a/x.md"}, "📖 ファイルを読む: x.md"),
    ("Glob", {"pattern": "*"}, "🔍 ファイルを探す"),
    ("Bash", {"command": "ls", "description": "一覧"}, "⌨ コマンドを実行: 一覧"),
    (CAL + "create_event", {"summary": "ジム"}, "📅 予定を作成: ジム"),
    (CAL + "mystery_tool", {}, "📅 カレンダー: mystery_tool"),
    ("FancyNewTool", {}, "🔧 FancyNewTool"),
])
def test_labels_in_japanese(name, inp, label):
    assert tool_label(name, inp, "ja") == label


def test_every_english_label_has_a_japanese_one():
    from zundamon_jarvis import labels

    used = {label for _, label in labels._FILE_TOOLS.values()} | {label for _, label in labels._PLAIN.values()}
    used |= {display for _, _, display, _ in labels._MCP} | {label for *_, rules in labels._MCP for _, label, _ in rules}
    used |= {"Web search", "Read page", "Start background task", "Ask subagent", "Run command", "Use skill"}
    assert used <= set(labels.TEXT)
    assert all(labels.TEXT[k]["en"] == k for k in labels.TEXT)
