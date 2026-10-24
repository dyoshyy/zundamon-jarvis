"""`zundamon-jarvis voices`: read a sample line with candidate male voices."""

import asyncio
import sys

from zundamon_jarvis.player import Player
from zundamon_jarvis.tts import Voicevox

CANDIDATES = ["青山龍星", "玄野武宏", "剣崎雌雄", "白上虎太郎"]
SAMPLE = "ご用件をどうぞ。本日の予定を確認しました。"
# VOICEVOX /speakers has no gender field, so candidates are listed by name.


async def run(play: bool = True) -> None:
    vv = Voicevox()
    try:
        speakers = await vv.speakers()
        by_name = {s["name"]: s for s in speakers}
        names = [n for n in CANDIDATES if n in by_name]
        player = Player() if play else None
        for name in names:
            for st in by_name[name]["styles"]:
                print(f"{name} / {st['name']}  id={st['id']}", flush=True)
                if player:
                    pcm = await vv.synth(SAMPLE, st["id"])
                    try:
                        await asyncio.wait_for(player.play(pcm, 24000), timeout=15)
                    except Exception as e:  # no output device etc.
                        print(f"  (playback failed: {e})", file=sys.stderr)
                        player = None
    finally:
        await vv.aclose()


def main(argv: list[str] | None = None) -> int:
    """`zundamon-jarvis voices [--no-play]` (dispatched by zundamon_jarvis.cli)."""
    args = sys.argv[2:] if argv is None else argv
    if any(a != "--no-play" for a in args):
        print("usage: zundamon-jarvis voices [--no-play]", file=sys.stderr)
        return 2
    asyncio.run(run(play="--no-play" not in args))
    return 0
