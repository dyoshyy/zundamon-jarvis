"""Turn a stream of 32ms mic frames + VAD probabilities into speech events.

Onset after ``start_ms`` of speech, end after ``end_silence_ms`` of silence, or a forced cut
(``Utterance.cut``) once an utterance reaches ``max_utterance_s`` (default 30 s).
"""

import math
from collections import deque
from dataclasses import dataclass

import numpy as np

FRAME_SAMPLES = 512  # 32ms at 16kHz
FRAME_MS = 32


@dataclass
class SpeechStarted:
    pass


@dataclass
class Utterance:
    audio: np.ndarray  # float32, 16kHz, mono
    cut: bool = False  # ended by max_utterance_s, not by silence


def _frames(ms: int) -> int:
    return max(1, math.ceil(ms / FRAME_MS))


class Endpointer:
    def __init__(
        self,
        start_ms: int = 250,
        end_silence_ms: int = 1500,
        threshold: float = 0.5,
        preroll_ms: int = 300,
        max_utterance_s: float = 30.0,
    ):
        # A VAD that never drops below the threshold (noise, a stuck state) would otherwise
        # keep one utterance open forever: nothing heard until pause/resume. Cut it instead.
        self.max_utterance_s = max_utterance_s
        self._max_frames = _frames(int(max_utterance_s * 1000))
        self._start_frames = _frames(start_ms)
        self._end_frames = _frames(end_silence_ms)
        self._threshold = threshold
        self._preroll: deque[np.ndarray] = deque(maxlen=_frames(preroll_ms))
        self.reset()

    def reset(self) -> None:
        self._preroll.clear()
        self._candidate: list[np.ndarray] = []  # consecutive speech frames before onset
        self._utterance: list[np.ndarray] = []
        self._silence = 0
        self.in_speech = False

    @property
    def utterance_s(self) -> float:
        """Length of the utterance under way (0 when not in speech)."""
        return sum(len(f) for f in self._utterance) / 16000 if self.in_speech else 0.0

    def feed(self, frame: np.ndarray, prob: float) -> list[SpeechStarted | Utterance]:
        frame = np.asarray(frame, dtype=np.float32)
        is_speech = prob >= self._threshold
        events: list[SpeechStarted | Utterance] = []

        if not self.in_speech:
            if not is_speech:
                # blip (if any) was not speech: return it to the preroll history
                self._preroll.extend(self._candidate)
                self._candidate = []
                self._preroll.append(frame)
                return events
            self._candidate.append(frame)
            if len(self._candidate) >= self._start_frames:
                self.in_speech = True
                self._utterance = [*self._preroll, *self._candidate]
                self._preroll.clear()
                self._candidate = []
                self._silence = 0
                events.append(SpeechStarted())
            return events

        self._utterance.append(frame)
        if len(self._utterance) >= self._max_frames:
            events.append(Utterance(np.concatenate(self._utterance), cut=True))
            self._utterance = []
            self._silence = 0
            self.in_speech = False
            return events
        if is_speech:
            self._silence = 0
        else:
            self._silence += 1
            if self._silence >= self._end_frames:
                events.append(Utterance(np.concatenate(self._utterance)))
                self._utterance = []
                self._silence = 0
                self.in_speech = False
        return events
