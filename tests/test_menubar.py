from zundamon_jarvis import cli, menubar


def labels(model):
    return [(i.label, i.action, i.enabled) for i in model.items]


def test_menu_listening():
    m = menubar.menu_model({"state": "listening", "text": "Listening"})
    assert m.title == "▶"
    assert m.note is None
    assert labels(m) == [
        ("Listening", None, False),
        ("Pause", "pause", True),
        ("Conversation…", "conversation", True),
        ("Settings…", "settings", True),
        ("Quit menu bar (daemon keeps running)", "quit", True),
    ]


def test_menu_paused_offers_resume():
    m = menubar.menu_model({"state": "paused", "text": "Paused"})
    assert m.title == "⏸"
    assert ("Resume", "resume", True) in labels(m)
    assert not any(i.label == "Pause" for i in m.items)


def test_every_state_has_a_one_char_title():
    for state in ("listening", "paused", "thinking", "speaking", "limit", "offline"):
        assert len(menubar.menu_model({"state": state, "text": state}).title) == 1


def test_menu_when_daemon_down():
    m = menubar.menu_model(None)
    assert m.items[0].label == "Not running" and not m.items[0].enabled
    actions = {i.action: i.enabled for i in m.items[1:]}
    assert actions == {"pause": False, "conversation": False, "settings": False, "quit": True}
    assert len(m.title) == 1


def test_menu_explains_mic_permission():
    m = menubar.menu_model({"state": "offline", "text": "Offline (mic)"})
    assert "システム設定 > プライバシーとセキュリティ > マイク" in m.note
    assert m.items[0].label == "Offline (mic)"


def test_menu_voice_offline_is_not_a_mic_problem():
    m = menubar.menu_model({"state": "offline", "text": "Offline (voice)"})
    assert m.note is None or "マイク" not in m.note


def test_menu_shows_login_problem():
    m = menubar.menu_model({"state": "listening", "text": "Listening", "login": False})
    assert "ターミナルで `claude` を実行してログイン" in m.note


def test_menu_login_true_or_absent_has_no_note():
    assert menubar.menu_model({"state": "listening", "text": "Listening", "login": True}).note is None


def test_both_notes_are_kept():
    m = menubar.menu_model({"state": "offline", "text": "Offline (mic)", "login": False})
    assert "マイク" in m.note and "claude" in m.note


def test_cli_dispatches_menubar(monkeypatch):
    seen = []
    monkeypatch.setattr("zundamon_jarvis.menubar.run_menubar", lambda: seen.append(1) or 0)
    assert cli.main(["menubar"]) == 0
    assert seen == [1]
