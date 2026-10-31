from zundamon_jarvis import cli, i18n, menubar


def labels(model):
    return [(i.label, i.action, i.enabled) for i in model.items]


def test_menu_listening():
    m = menubar.menu_model({"state": "listening", "text": "Listening"}, lang="en")
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
    m = menubar.menu_model({"state": "paused", "text": "Paused"}, lang="en")
    assert m.title == "⏸"
    assert ("Resume", "resume", True) in labels(m)
    assert not any(i.label == "Pause" for i in m.items)


def test_every_state_has_a_one_char_title():
    for state in ("listening", "paused", "thinking", "speaking", "limit", "offline"):
        assert len(menubar.menu_model({"state": state, "text": state}).title) == 1


def test_menu_when_daemon_down():
    m = menubar.menu_model(None, lang="en")
    assert m.items[0].label == "Not running" and not m.items[0].enabled
    actions = {i.action: i.enabled for i in m.items[1:]}
    assert actions == {"pause": False, "conversation": False, "settings": False, "quit": True}
    assert len(m.title) == 1


def test_menu_explains_mic_permission():
    m = menubar.menu_model({"state": "offline", "text": "Offline (mic)", "reasons": ["mic"]})
    assert "システム設定 > プライバシーとセキュリティ > マイク" in m.note
    assert m.items[0].label == "オフライン（マイク）"
    en = menubar.menu_model({"state": "offline", "text": "Offline (mic)", "reasons": ["mic"]}, lang="en")
    assert "System Settings > Privacy & Security > Microphone" in en.note and "python3.12" in en.note
    assert en.items[0].label == "Offline (mic)"


def test_an_older_daemon_without_reasons_still_gets_the_mic_note():
    assert "python3.12" in menubar.menu_model({"state": "offline", "text": "Offline (mic)"}).note
    assert menubar.menu_model({"state": "offline", "text": "Offline (voice)", "reasons": ["tts"]}).note is None


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


def test_windows_model_has_the_tray_quit_label_and_windows_mic_advice():
    m = menubar.menu_model({"state": "offline", "text": "Offline (mic)", "reasons": ["mic"]}, "windows")
    assert m.items[-1] == menubar.MenuItem("トレイを終了（本体は動き続けます）", "quit")
    assert "デスクトップ アプリがマイクにアクセス" in m.note and "python3.12" not in m.note
    en = menubar.menu_model({"state": "offline", "reasons": ["mic"]}, "windows", lang="en")
    assert en.items[-1] == menubar.MenuItem("Quit tray (daemon keeps running)", "quit")
    assert "Let desktop apps access your microphone" in en.note
    assert menubar.menu_model(None, "windows", lang="en").items[-1].label.startswith("Quit tray")


def test_menu_in_japanese_and_english():
    status = {"state": "listening", "text": "Listening", "login": False}
    ja = menubar.menu_model(status, lang="ja")
    assert labels(ja) == [
        ("聞き取り中", None, False),
        ("一時停止", "pause", True),
        ("会話…", "conversation", True),
        ("設定…", "settings", True),
        ("メニューバーを終了（本体は動き続けます）", "quit", True),
    ]
    assert ja.note == menubar.TEXT["login"]["ja"]
    en = menubar.menu_model(status, lang="en")
    assert [i.action for i in en.items] == [i.action for i in ja.items]
    assert en.note == "Not signed in to Claude. Run `claude` in a terminal to sign in"
    assert menubar.menu_model({"state": "paused"}, lang="ja").items[1] == menubar.MenuItem("再開", "resume")
    assert menubar.menu_model(None, lang="ja").items[0].label == "停止中"


def test_menu_follows_the_display_language_on_every_draw(monkeypatch):
    monkeypatch.setattr(i18n, "language", lambda config_path=None: "en")
    assert menubar.menu_model(None).items[1].label == "Pause"
    monkeypatch.setattr(i18n, "language", lambda config_path=None: "ja")
    assert menubar.menu_model(None).items[1].label == "一時停止"
