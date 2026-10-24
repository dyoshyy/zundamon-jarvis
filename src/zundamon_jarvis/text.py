"""Pure text helpers for voice output and STT filtering."""

import re
import unicodedata

_SENTENCE_END = "。！？!?"


class SentenceChunker:
    """Accumulates streamed text and emits complete sentences."""

    def __init__(self) -> None:
        self._buf = ""

    def feed(self, delta: str) -> list[str]:
        self._buf += delta
        out: list[str] = []
        start = 0
        for i, ch in enumerate(self._buf):
            if ch in _SENTENCE_END:
                sentence = self._buf[start : i + 1].strip()
                start = i + 1
            elif ch == "\n":
                sentence = self._buf[start:i].strip()
                start = i + 1
            else:
                continue
            if sentence:
                out.append(sentence)
        self._buf = self._buf[start:]
        return out

    def flush(self) -> list[str]:
        rest = self._buf.strip()
        self._buf = ""
        return [rest] if rest else []


_URL = re.compile(r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+")
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_LINE_PREFIX = re.compile(r"^\s*(?:#{1,6}\s+|>\s+|[-*+]\s+|\d+\.\s+)")


def speakable(text: str) -> str:
    text = _LINK.sub(r"\1", text)
    text = _URL.sub("リンク", text)
    lines = []
    for line in text.split("\n"):
        line = _LINE_PREFIX.sub("", line)
        line = re.sub(r"^\s*(?:#{1,6}|[-*+])\s*$", "", line)
        lines.append(line)
    text = "\n".join(lines)
    text = text.replace("|", " ").replace("#", "").replace("*", "").replace("`", "")
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


SILENT_TOKEN = "[[silent]]"


def is_silent(reply: str) -> bool:
    return reply.strip() == SILENT_TOKEN


_HALLUCINATIONS = frozenset(
    {
        "ご視聴ありがとうございました",
        "ご視聴ありがとうございます",
        "チャンネル登録よろしくお願いします",
        "チャンネル登録お願いします",
        "ご清聴ありがとうございました",
        "最後までご視聴いただきありがとうございます",
        "最後までご視聴いただきありがとうございました",
        "字幕は自動生成されています",
        "字幕視聴ありがとうございました",
        "またお会いしましょう",
        "次の動画でお会いしましょう",
        "高評価とチャンネル登録お願いします",
    }
)


def _strip_punct(text: str) -> str:
    return "".join(
        ch for ch in text if not ch.isspace() and not unicodedata.category(ch).startswith("P")
    )


def is_hallucination(text: str) -> bool:
    core = _strip_punct(text)
    return len(core) <= 1 or core in _HALLUCINATIONS
