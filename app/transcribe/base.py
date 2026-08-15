"""The transcription interface, and the audio extraction every backend needs.

Mirrors ``app.tts.base``: an interface plus a registry, so a second engine can
be added without the UI knowing which one is in use.

Transcription is the mirror image of what the rest of the app does. Everywhere
else the SRT is the master clock and speech is fitted to it; here there is no
clock yet, and the audio is what defines one.
"""

from __future__ import annotations

import logging
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from app.core.errors import StudioError

logger = logging.getLogger(__name__)

#: Whisper models are trained on 16 kHz mono. Feeding anything else means the
#: library resamples internally, so extract it in the right shape once.
SAMPLE_RATE = 16_000

VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".webm"}
AUDIO_SUFFIXES = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus"}
MEDIA_SUFFIXES = VIDEO_SUFFIXES | AUDIO_SUFFIXES


@dataclass(frozen=True)
class Utterance:
    """One recognised stretch of speech, with the times it occupies."""

    start_ms: int
    end_ms: int
    text: str

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


@dataclass
class TranscriptionResult:
    utterances: list[Utterance]
    language: str = ""
    language_probability: float = 0.0
    duration_ms: int = 0
    model: str = ""
    seconds_taken: float = 0.0
    warnings: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.utterances


@dataclass(frozen=True)
class TranscribeRequest:
    media_path: Path
    #: Empty means "detect it".
    language: str = ""
    model_size: str = "small"


#: Called with ``(fraction, message)``.
#:
#: The fraction distinguishes the two kinds of message, and callers rely on it:
#: ``None`` means ``message`` is a status line about what is happening, and a
#: number means ``message`` is recognised speech at that point through the
#: audio. Without that, stage messages end up printed in the transcript as if
#: someone had said them.
ProgressCallback = Callable[[float | None, str], None]


class Transcriber(ABC):
    identifier: str = "base"
    display_name: str = "Base transcriber"

    @abstractmethod
    def is_available(self) -> tuple[bool, str]:
        """Return ``(available, reason)``; reason is empty when available."""

    @abstractmethod
    def transcribe(
        self,
        request: TranscribeRequest,
        on_progress: ProgressCallback | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> TranscriptionResult:
        """Turn media into timed utterances."""


# -- audio extraction ----------------------------------------------------


def media_duration_ms(path: Path) -> int:
    """Length of a media file in milliseconds, or 0 if it cannot be read."""
    from app.audio.media import probe

    return probe(path).duration_ms


def has_audio_track(path: Path) -> bool:
    """Whether the file contains any audio at all.

    Worth checking before spending minutes on a silent screen recording, which
    is exactly the file someone is most likely to try first.
    """
    from app.audio.media import probe

    info = probe(path)
    # If the file could not be read at all, do not block the user here; let the
    # extraction produce the real error, which explains far more.
    return True if not info.readable else info.has_audio


def extract_audio(source: Path, destination: Path | None = None) -> Path:
    """Write ``source``'s audio as 16 kHz mono WAV, the shape Whisper expects."""
    from app.audio.media import decode_to_wav

    if destination is None:
        directory = Path(tempfile.mkdtemp(prefix="narration_transcribe_"))
        destination = directory / f"{source.stem}-16k.wav"
    return decode_to_wav(source, destination, rate=SAMPLE_RATE)


# -- conversion ----------------------------------------------------------

#: Below this a caption is unreadable and, more importantly, gives TTS no room.
MIN_CAPTION_MS = 400


def to_segments(utterances: Sequence[Utterance]) -> list["Segment"]:
    """Turn recognised speech into the caption timeline the rest of the app uses.

    Whisper occasionally returns times that overlap, run backwards, or collapse
    to nothing. The document model assumes a clean ascending timeline, so the
    tidying happens once, here, rather than being defended against everywhere
    downstream.
    """
    from app.core.models import Segment

    segments: list[Segment] = []
    previous_end = 0
    for utterance in sorted(utterances, key=lambda u: (u.start_ms, u.end_ms)):
        text = utterance.text.strip()
        if not text:
            continue
        start = max(0, utterance.start_ms, previous_end)
        end = max(utterance.end_ms, start + MIN_CAPTION_MS)
        segments.append(Segment(start_ms=start, end_ms=end, text=text))
        previous_end = end
    return segments


# -- registry ------------------------------------------------------------

_FACTORIES: dict[str, Callable[[], Transcriber]] = {}
_INSTANCES: dict[str, Transcriber] = {}


def register(identifier: str, factory: Callable[[], Transcriber]) -> None:
    _FACTORIES[identifier] = factory


def transcriber(identifier: str = "whisper") -> Transcriber:
    if identifier not in _FACTORIES:
        raise StudioError(
            f"No transcriber named “{identifier}”.",
            suggestion="This is a bug; please report it.",
        )
    if identifier not in _INSTANCES:
        _INSTANCES[identifier] = _FACTORIES[identifier]()
    return _INSTANCES[identifier]


def transcriber_ids() -> list[str]:
    return sorted(_FACTORIES)
