"""VOICEVOX engine client."""

import httpx
import numpy as np

DEFAULT_URL = "http://127.0.0.1:50021"
RATE = 24000
DEFAULT_SPEAKER = "青山龍星"


def talk_styles(speakers: list[dict]) -> list[tuple[str, str, int]]:
    """(speaker, style, id) for every speaking style in a ``/speakers`` reply, in engine order.
    Singing styles (no ``audio_query``) and malformed entries are skipped."""
    out = []
    for sp in speakers if isinstance(speakers, list) else ():
        if not isinstance(sp, dict) or not isinstance(sp.get("name"), str):
            continue
        for st in sp.get("styles") or ():
            if not isinstance(st, dict):
                continue
            sid, kind = st.get("id"), st.get("type", "talk")
            if isinstance(sid, int) and not isinstance(sid, bool) and "talk" in str(kind):
                out.append((sp["name"], str(st.get("name", "")), sid))
    return out


class Voicevox:
    def __init__(
        self,
        base_url: str = DEFAULT_URL,
        speaker_id: int = 0,
        speed: float = 1.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.speaker_id = speaker_id
        self.speed = speed  # audio_query speedScale; changed live by the settings window
        self._http = httpx.AsyncClient(base_url=base_url, timeout=60, transport=transport)

    async def version(self) -> str:
        """Cheap reachability check."""
        r = await self._http.get("/version", timeout=5)
        r.raise_for_status()
        return r.json()

    async def speakers(self) -> list[dict]:
        r = await self._http.get("/speakers")
        r.raise_for_status()
        return r.json()

    async def find_speaker(self, name: str, style: str = "ノーマル") -> int:
        for sp in await self.speakers():
            if sp["name"] == name:
                for st in sp["styles"]:
                    if st["name"] == style:
                        return st["id"]
        raise LookupError(f"speaker not found: {name} / {style}")

    async def synth(
        self, text: str, speaker_id: int | None = None, *, speed: float | None = None
    ) -> np.ndarray:
        sid = self.speaker_id if speaker_id is None else speaker_id
        q = await self._http.post("/audio_query", params={"text": text, "speaker": sid})
        q.raise_for_status()
        query = q.json()
        query["speedScale"] = self.speed if speed is None else speed
        s = await self._http.post("/synthesis", params={"speaker": sid}, json=query)
        s.raise_for_status()
        return wav_to_float32(s.content)

    async def aclose(self) -> None:
        await self._http.aclose()


def wav_to_float32(data: bytes) -> np.ndarray:
    import io
    import wave

    with wave.open(io.BytesIO(data)) as w:
        assert w.getsampwidth() == 2
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        if w.getnchannels() > 1:
            pcm = pcm.reshape(-1, w.getnchannels())[:, 0]
    return pcm.astype(np.float32) / 32768.0
