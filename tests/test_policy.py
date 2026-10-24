from pathlib import Path

import pytest

from zundamon_jarvis.policy import describe_action, needs_confirmation, parse_confirmation

R = Path("/home/alice/notes")


def bash(cmd: str) -> bool:
    return needs_confirmation("Bash", {"command": cmd}, R)


# ---- Bash: deleting files or data asks ----

@pytest.mark.parametrize("cmd", [
    "rm -rf build",
    "rm x",
    "rmdir empty",
    "shred -u secret.txt",
    "unlink link",
    "find . -name '*.tmp' -delete",
    "find . -type f -exec rm {} \\;",
    "find . -exec rm -f {} +",
    "find . -execdir rm {} \\;",
    "find . -ok rm {} \\;",
    "git clean -fd",
    "git -C sub clean -fdx",
    "git reset --hard HEAD~1",
    "git reset --hard",
    "git branch -D feature",
    "git branch --delete --force feature",
    "git branch -df feature",
    "git push origin --delete feature",
    "git push -d origin feature",
    "git push origin :feature",
    "trash old.txt",
    "trash-put old.txt",
    "gio trash old.txt",
    "gio remove old.txt",
    "truncate -s 0 app.log",
    "dd if=/dev/zero of=/dev/sda bs=1M",
    "mkfs.ext4 /dev/sdb1",
    "mkfs -t ext4 /dev/sdb1",
])
def test_bash_delete_asks(cmd):
    assert bash(cmd)


# Same deletions, hidden behind operators, wrappers, quoting and substitutions.
@pytest.mark.parametrize("cmd", [
    "ls; rm x",
    "ls && rm x",
    "false || rm x",
    "ls & rm x",
    "ls\nrm x",
    "cd /tmp; rm -rf build",
    "ls | xargs rm",
    "find . -name '*.log' | xargs -I{} rm {}",
    "find . -name '*.log' | xargs -I {} rm {}",
    "find . -print0 | xargs -0 -n 1 rm -f",
    "echo $(rm x)",
    "echo `rm x`",
    'echo "$(rm x)"',
    "cat <(rm x)",
    "(cd build && rm -rf *)",
    "{ rm x; }",
    "if true; then rm x; fi",
    'for f in *.log; do rm "$f"; done',
    "while read f; do rm \"$f\"; done < list",
    "bash -c 'rm -rf build'",
    'sh -c "ls; rm x"',
    "zsh -lc 'rm x'",
    "eval rm x",
    "eval 'rm x'",
    "env rm x",
    "env FOO=1 rm x",
    "env -i PATH=/bin rm x",
    "FOO=1 rm x",
    "/bin/rm x",
    "/usr/bin/rm -rf x",
    "command rm x",
    "builtin command rm x",
    "exec rm x",
    "\\rm x",
    "'rm' x",
    '"rm" x',
    "r\\m x",
    "$'rm' x",
    "r''m x",
    "nohup rm -rf x &",
    "nice -n 10 rm x",
    "timeout 5 rm x",
    "time rm x",
    "watch -n 1 rm x",
    "busybox rm x",
    "ssh host 'rm -rf /tmp/x'",
    "ssh -p 22 host rm -rf /tmp/x",
    "watch 'rm x'",
    'bash -c "rm a"',
    "bash -c rm",
    "env -S 'rm x'",
    'echo "$(rm x)"',
    'echo "`rm x`"',
    'echo "a $(echo "b"; rm x) c"',
    'echo "$(echo $(rm x))"',
    "find . -exec sh -c 'rm \"$1\"' _ {} \\;",
    "ls | xargs sh -c 'rm x'",
    'flock /tmp/l -c "rm x"',
    "timeout 5 bash -c 'rm x'",
    "ls 2>/dev/null; rm x",
    "echo done >log&&rm x",
    "git commit -m wip && git reset --hard origin/main",
])
def test_bash_delete_evasions_ask(cmd):
    assert bash(cmd)


# ---- Bash: privilege and force push ask ----

@pytest.mark.parametrize("cmd", [
    "sudo ls",
    "sudo -u root ls",
    "sudo pacman -Syu",
    "echo x | sudo tee /etc/hosts",
    "ls && sudo systemctl restart foo",
    "env sudo ls",
    "/usr/bin/sudo ls",
    "bash -c 'sudo ls'",
    "echo $(sudo cat /etc/shadow)",
    "doas ls",
    "pkexec ls",
    "run0 ls",
    "su -c 'ls' root",
])
def test_bash_privilege_asks(cmd):
    assert bash(cmd)


@pytest.mark.parametrize("cmd", [
    "git push --force",
    "git push -f origin main",
    "git push --force-with-lease",
    "git push --force-with-lease=main:abc origin main",
    "git push -fu origin main",
    "git push origin +main",
    "git -C repo push --force",
    "git push origin HEAD:+main",
    "git push origin +HEAD:main",
    "git push --mirror",
    "git push --mirror origin",
    "git commit -m x && git push --force",
    "bash -c 'git push -f'",
])
def test_bash_force_push_asks(cmd):
    assert bash(cmd)


# ---- Bash: everything else runs without asking ----

@pytest.mark.parametrize("cmd", [
    "find . -name x",
    "find notes -name '*.md'",
    "find . -name '*.py' -exec grep -l foo {} +",
    "ls | wc -l",
    "ls -la",
    "ls *.py",
    "git status",
    "git log --oneline -5",
    "git diff HEAD~1",
    "git commit -m x",
    "git commit -m 'remove old notes'",
    'git commit -m "rm old"',
    "git commit -m 'a; rm b'",
    "git commit -m 'fix; sudo x && git push --force'",
    'echo "rm foo"',
    'gh pr create --title "rm x" --body "drop; rm -rf build"',
    "echo 'echo $(rm x)'",
    "echo '`rm x`'",
    "cat <<< 'rm x'",
    "git push origin HEAD:main",
    "git push origin main:refs/heads/main",
    "git add -A && git commit -m 'fix: x'",
    "git push",
    "git push origin main",
    "git push -u origin feature",
    "git branch -d merged",
    "git branch -m old new",
    "git reset --soft HEAD~1",
    "git reset HEAD file.txt",
    "git checkout -b feature",
    "mkdir -p a && cp x a/",
    "cp -r a b",
    "mv a b",
    "echo hi > out.txt",
    "echo hi >> log.txt",
    "echo hi > rm",
    "ls 2>/dev/null",
    "ls > out.txt 2>&1",
    "cat <<EOF > notes.txt\nhello\nEOF",
    "npm install",
    "npm run build && npm test",
    "python script.py",
    "python -c 'print(1)'",
    "uv run pytest -q 2>&1 | tail -3",
    "grep -r rm src",
    "cat x | grep rm",
    'ls | grep "rm"',
    "man rm",
    "which rm",
    "command -v rm",
    "echo 'use rm carefully'",
    "sed -i 's/x/rm/' file.txt",
    "dd if=disk.img bs=1M count=1",
    "for f in *.md; do echo $f; done",
    "curl -s https://example.com | jq .",
    "docker ps",
    "git status; git diff",
    "grep -c '#' notes/x.md",
    "ls a#; echo b",
    "chmod +x run.sh && ./run.sh",
    "cat 'unterminated",
    "",
])
def test_bash_plain_commands_do_not_ask(cmd):
    assert not bash(cmd)


def test_bash_without_command_does_not_ask():
    assert not needs_confirmation("Bash", {}, R)


# ---- Monitor uses the same command rules ----

@pytest.mark.parametrize("inp,exp", [
    ({"command": "tail -f log | grep --line-buffered ERROR", "description": "x"}, False),
    ({"command": "tail -f log | sh", "description": "x"}, False),
    ({"ws": {"url": "wss://example.com/s"}, "description": "x"}, False),
    ({"description": "no command"}, False),
    ({"command": "while true; do rm -f /tmp/x; sleep 1; done", "description": "x"}, True),
    ({"command": "sudo journalctl -f", "description": "x"}, True),
])
def test_monitor(inp, exp):
    assert needs_confirmation("Monitor", inp, R) is exp


# ---- File tools: anywhere, no questions ----

@pytest.mark.parametrize("tool,inp", [
    ("Write", {"file_path": "/home/alice/.bashrc"}),
    ("Edit", {"file_path": "/home/alice/.claude/settings.json"}),
    ("Write", {"file_path": str(R / "notes/memo.md")}),
    ("Write", {"file_path": str(R / "../zundamon-jarvis/x.py")}),
    ("Edit", {"file_path": str(R / ".git/config")}),
    ("Write", {"file_path": str(R / ".claude/settings.json")}),
    ("Write", {"file_path": str(R / ".mcp.json")}),
    ("MultiEdit", {"file_path": "/etc/hosts"}),
    ("NotebookEdit", {"notebook_path": "/tmp/n.ipynb"}),
    ("Write", {}),
    ("Write", {"file_path": str(R / "a\x00b.md")}),
    ("Edit", {"file_path": "~nosuchuser_xyz/x"}),
    ("Read", {"file_path": "/etc/hosts"}),
])
def test_file_tools_do_not_ask(tool, inp):
    assert not needs_confirmation(tool, inp, R)


# ---- Built-ins: only the outward ones ask ----

@pytest.mark.parametrize("tool,inp", [
    ("RemoteTrigger", {}),
    ("PushNotification", {}),
    ("Artifact", {"action": "delete", "url": "https://claude.ai/artifact/x"}),
    ("ArtifactData", {"action": "delete", "collection": "c", "doc_id": "d"}),
])
def test_outward_builtins_ask(tool, inp):
    assert needs_confirmation(tool, inp, R)


@pytest.mark.parametrize("tool", [
    "Glob", "Grep", "WebFetch", "WebSearch", "Agent", "Task", "Skill", "ToolSearch", "TodoWrite",
    "LSP", "TaskStop", "ExitPlanMode", "AskUserQuestion", "CronCreate", "CronDelete", "DesignSync",
    "ExitWorktree", "EnterWorktree", "Workflow", "SomeFutureTool", "Artifact", "ArtifactData",
])
def test_other_builtins_do_not_ask(tool):
    assert not needs_confirmation(tool, {}, R)


def test_artifact_publish_does_not_ask():
    assert not needs_confirmation("Artifact", {"action": "publish", "file_path": "/tmp/x.html"}, R)


# ---- MCP: only send / calendar / delete / money / SQL ask ----

@pytest.mark.parametrize("tool", [
    # sending mail or messages to people
    "mcp__claude_ai_Gmail__send_message",
    "mcp__claude_ai_Gmail__reply",
    "mcp__claude_ai_Gmail__forward",
    "mcp__claude_ai_Google_Drive__share_file",
    "mcp__claude_ai_Notion__notion-create-comment",
    "mcp__claude_ai_Notion__notion-update-comment",
    "mcp__claude_ai_Notion__notion-send-message-to-session",
    "mcp__claude_ai_Slack__slack_send_message",
    "mcp__claude_ai_Slack__slack_post_message",
    "mcp__claude_ai_Slack__slack_reply_to_thread",
    "mcp__claude_ai_Slack__slack_schedule_message",
    "mcp__claude_ai_Slack__chat_postMessage",
    # calendar changes
    "mcp__claude_ai_Google_Calendar__create_event",
    "mcp__claude_ai_Google_Calendar__update_event",
    "mcp__claude_ai_Google_Calendar__delete_event",
    "mcp__claude_ai_Google_Calendar__respond_to_event",
    "mcp__claude_ai_Notion__notion-calendar-create-events",
    "mcp__claude_ai_Notion__notion-calendar-update-events",
    "mcp__claude_ai_Notion__notion-calendar-cancel-events",
    # deleting data
    "mcp__claude_ai_Gmail__trash_message",
    "mcp__claude_ai_Gmail__trash_thread",
    "mcp__claude_ai_Gmail__delete_draft",
    "mcp__claude_ai_Gmail__delete_label",
    "mcp__claude_ai_Google_Drive__trash_file",
    "mcp__claude_ai_Notion__notion-delete-pages",
    "mcp__claude_ai_Notion__notion-delete-databases",
    "mcp__claude_ai_Notion__notion-delete-comment",
    "mcp__claude_ai_Claude_Docs__delete",
    "mcp__Neon__delete_branch",
    "mcp__Neon__delete_project",
    "mcp__Neon__delete_storage_objects_by_prefix",
    "mcp__Neon__reset_from_parent",
    "mcp__some__dropTable",
    "mcp__some__remove_item",
    # SQL can delete data
    "mcp__Neon__run_sql",
    "mcp__Neon__run_sql_transaction",
    "mcp__Neon__explain_sql_statement",
    "mcp__Neon__complete_database_migration",
    # money
    "mcp__claude_ai_freee__freee_api_post",
    "mcp__claude_ai_freee__freee_api_put",
    "mcp__claude_ai_freee__freee_api_patch",
    "mcp__claude_ai_freee__freee_api_delete",
    "mcp__shop__purchase_item",
    "mcp__stripe__create_payment",
    "mcp__bank__transfer_funds",
])
def test_mcp_confirm_categories_ask(tool):
    assert needs_confirmation(tool, {}, R)


@pytest.mark.parametrize("tool", [
    "mcp__claude_ai_Google_Calendar__list_events",
    "mcp__claude_ai_Google_Calendar__get_event",
    "mcp__claude_ai_Google_Calendar__search_events",
    "mcp__claude_ai_Google_Calendar__suggest_time",
    "mcp__claude_ai_Google_Calendar__list_calendars",
    "mcp__claude_ai_Gmail__search_threads",
    "mcp__claude_ai_Gmail__get_thread",
    "mcp__claude_ai_Gmail__get_message",
    "mcp__claude_ai_Gmail__create_draft",
    "mcp__claude_ai_Gmail__update_draft",
    "mcp__claude_ai_Gmail__label_message",
    "mcp__claude_ai_Gmail__update_message_labels",
    "mcp__claude_ai_Gmail__untrash_thread",
    "mcp__claude_ai_Gmail__create_label",
    "mcp__claude_ai_Google_Drive__create_file",
    "mcp__claude_ai_Google_Drive__update_file",
    "mcp__claude_ai_Google_Drive__copy_file",
    "mcp__claude_ai_Google_Drive__read_file_content",
    "mcp__claude_ai_Notion__notion-create-pages",
    "mcp__claude_ai_Notion__notion-update-page",
    "mcp__claude_ai_Notion__notion-move-pages",
    "mcp__claude_ai_Notion__notion-restore-pages",
    "mcp__claude_ai_Notion__notion-get-comments",
    "mcp__claude_ai_Notion__notion-fetch",
    "mcp__claude_ai_Notion__notion-calendar-list-events",
    "mcp__claude_ai_Claude_Docs__batch",
    "mcp__claude_ai_Claude_Docs__update",
    "mcp__Neon__create_branch",
    "mcp__Neon__list_branch_computes",
    "mcp__Neon__describe_project",
    "mcp__Neon__start_postgres_endpoint",
    "mcp__Neon__prepare_database_migration",
    "mcp__claude_ai_freee__freee_api_get",
    "mcp__claude_ai_freee__freee_api_list_paths",
    "mcp__claude_ai_freee__freee_set_current_company",
    "mcp__claude_ai_freee__freee_clear_auth",
    "mcp__claude-in-chrome__navigate",
    "mcp__claude-in-chrome__tabs_close_mcp",
    "mcp__stripe__list_payments",
    "mcp__postgres__query",
    "mcp__x__do_thing",
])
def test_mcp_other_tools_do_not_ask(tool):
    assert not needs_confirmation(tool, {}, R)


def test_malformed_mcp_name_asks():
    assert needs_confirmation("mcp__broken", {}, R)


# ---- describe_action: one plain spoken question ----

def test_describe_calendar_event():
    d = describe_action("mcp__claude_ai_Google_Calendar__create_event",
                        {"summary": "ジム", "start": {"dateTime": "2026-10-25T08:30:00+09:00"}})
    assert "ジム" in d and "10月25日" in d and d.endswith("いい？")


def test_describe_calendar_event_flat_start_time():
    d = describe_action("mcp__claude_ai_Google_Calendar__create_event",
                        {"summary": "テスト", "startTime": "2026-10-25T10:00:00"})
    assert "テスト" in d and "10月25日" in d and "10時" in d and d.endswith("いい？")


@pytest.mark.parametrize("cmd,expected", [
    ("rm -rf build", "rm で build を消すけど、いい？"),
    ("ls && rm x", "rm で x を消すけど、いい？"),
    ("sudo pacman -Syu", "sudo で pacman -Syu を実行するけど、いい？"),
    ("git push --force", "強制プッシュするけど、いい？"),
])
def test_describe_bash_exact(cmd, expected):
    assert describe_action("Bash", {"command": cmd}) == expected


@pytest.mark.parametrize("cmd,word", [
    ("find . -name '*.tmp' -delete", "消す"),
    ("git clean -fd", "消す"),
    ("git reset --hard HEAD~1", "捨てる"),
    ("git branch -D feature", "feature"),
    ("git push origin --delete feature", "消す"),
    ("mkfs.ext4 /dev/sdb1", "初期化"),
    ("dd if=/dev/zero of=/dev/sda", "上書き"),
    ("trash old.txt", "ゴミ箱"),
    ("truncate -s 0 app.log", "app.log"),
    ("rm -rf *", "消す"),
    ("echo hi", "実行"),
])
def test_describe_bash_categories(cmd, word):
    d = describe_action("Bash", {"command": cmd})
    assert word in d and d.endswith("いい？")


def test_describe_gmail_send_names_recipient():
    d = describe_action("mcp__claude_ai_Gmail__send_message", {"to": ["tanaka@example.com"], "subject": "見積"})
    assert "tanakaさんに" in d and "見積" in d and "送る" in d and d.endswith("いい？")


@pytest.mark.parametrize("tool,word", [
    ("mcp__claude_ai_Notion__notion-create-comment", "コメント"),
    ("mcp__claude_ai_Slack__slack_send_message", "メッセージ"),
    ("mcp__claude_ai_Google_Drive__trash_file", "ゴミ箱"),
    ("mcp__claude_ai_Notion__notion-delete-pages", "消す"),
    ("mcp__Neon__run_sql", "SQL"),
    ("mcp__claude_ai_freee__freee_api_post", "freee"),
    ("mcp__claude_ai_Google_Drive__share_file", "共有"),
    ("RemoteTrigger", "リモート"),
    ("PushNotification", "通知"),
    ("Artifact", "消す"),
])
def test_describe_other_categories(tool, word):
    d = describe_action(tool, {"action": "delete"} if tool == "Artifact" else {})
    assert word in d and d.endswith("いい？")


@pytest.mark.parametrize("tool,inp", [
    ("mcp__claude_ai_Google_Calendar__delete_event", {"eventId": "abc"}),
    ("mcp__claude_ai_Gmail__send_message", {"to": ["a_b@example.com"], "subject": "見積"}),
    ("mcp__claude_ai_Gmail__reply", {}),
    ("mcp__claude_ai_Gmail__trash_thread", {}),
    ("Write", {"file_path": "/home/alice/.bashrc"}),
    ("Bash", {"command": "rm -rf build/out_dir/*"}),
    ("Bash", {"command": "sudo cp a_b /etc/x#y"}),
    ("Bash", {"command": "find . -exec rm {} \\;"}),
    ("mcp__claude_ai_Notion__notion-update-page", {}),
    ("mcp__Neon__delete_branch", {}),
])
def test_describe_is_plain_spoken_question(tool, inp):
    d = describe_action(tool, inp)
    assert d.endswith("いい？")
    for bad in ("_", "/", "http", "*", "`", "#", "{", "}", "\\"):
        assert bad not in d


def test_describe_unknown_tool_falls_back():
    assert describe_action("mcp__x__do_thing", {}).endswith("いい？")


# ---- parse_confirmation ----

@pytest.mark.parametrize("t,exp", [("うん", "yes"), ("いいよ", "yes"), ("お願い", "yes"), ("OK", "yes"),
    ("だめ", "no"), ("やめて", "no"), ("ちょっと待って", "no"), ("いや", "no"),
    ("今日の天気どう？", "unclear")])
def test_parse_confirmation(t, exp):
    assert parse_confirmation(t) == exp


@pytest.mark.parametrize("t,exp", [
    ("はい、お願いします。", "yes"), ("オッケー", "yes"), ("ｏｋ", "yes"), ("うん、いいよ", "yes"),
    ("いいえ", "no"), ("いいや", "no"), ("ダメ", "no"), ("キャンセルで", "no"),
    ("うーん、やっぱりやめとく", "no"), ("", "unclear"),
    ("いいよ、あと明日の天気と来週の予定も全部教えてくれる？", "unclear"),  # long = not a plain yes
])
def test_parse_confirmation_more(t, exp):
    assert parse_confirmation(t) == exp
