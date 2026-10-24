import pytest

from zundamon_jarvis.text import SentenceChunker, is_hallucination, is_silent, speakable


def test_chunker_splits_on_japanese_punctuation():
    c = SentenceChunker()
    assert c.feed("いいね。次は") == ["いいね。"]
    assert c.feed("どうする？") == ["次はどうする？"]
    assert c.flush() == []


def test_chunker_flush_returns_remainder():
    c = SentenceChunker()
    c.feed("了解")
    assert c.flush() == ["了解"]


def test_chunker_splits_on_newline_and_ascii_marks():
    c = SentenceChunker()
    assert c.feed("one!\ntwo?three") == ["one!", "two?"]
    assert c.flush() == ["three"]


def test_speakable_strips_markdown_and_urls():
    assert speakable("**大事**なのは `x` です") == "大事なのは x です"
    assert speakable("詳しくは https://example.com を見て") == "詳しくは リンク を見て"
    assert speakable("- ") == ""


def test_speakable_headings_links_tables():
    assert speakable("# 見出し") == "見出し"
    assert speakable("- 項目") == "項目"
    assert speakable("[ドキュメント](https://example.com/a)を参照") == "ドキュメントを参照"
    assert speakable("| a | b |") == "a b"


def test_is_silent():
    assert is_silent("[[silent]]") and is_silent("  [[silent]]\n")
    assert not is_silent("うん")


@pytest.mark.parametrize("t", ["ご視聴ありがとうございました", "ご視聴ありがとうございました。",
                               "チャンネル登録よろしくお願いします", "。", "", "   ", "ん", "あ",
                               "ご清聴ありがとうございました", "最後までご視聴いただきありがとうございます",
                               "字幕は自動生成されています"])
def test_hallucinations_are_dropped(t):
    assert is_hallucination(t)


@pytest.mark.parametrize("t", ["あれ、なんだっけ", "よし", "明日の予定は？", "おやすみなさい"])
def test_real_speech_is_kept(t):
    assert not is_hallucination(t)


def test_speakable_url_followed_directly_by_japanese():
    assert speakable("詳しくはhttps://example.comを見て") == "詳しくはリンクを見て"
    assert speakable("https://a.com。") == "リンク。"
