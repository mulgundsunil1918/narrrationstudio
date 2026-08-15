"""The transcription interface, and the audio extraction every backend needs.

Mirrors ``app.tts.base``: an interface plus a registry, so a second engine can
be added without the UI knowing which one is in use.

Transcription is the mirror image of what the rest of the app does. Everywhere
else the SRT is the master clock and speech is fitted to it; here there is no
clock yet, and the audio is what defines one.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from app.core.errors import AudioError, StudioError

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


def media_duration_ms(path: Path, ffprobe: str = "ffprobe") -> int:
    """Length of a media file in milliseconds, or 0 if it cannot be read."""
    try:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, check=True, timeout=60,
        )
        return int(round(float(result.stdout.decode().strip()) * 1000))
    except Exception:
        return 0


def has_audio_track(path: Path, ffprobe: str = "ffprobe") -> bool:
    """Whether the file contains any audio at all.

    Worth checking before spending minutes on a silent screen recording, which
    is exactly the file someone is most likely to try first.
    """
    try:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=codec_type",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, check=True, timeout=60,
        )
        return b"audio" in result.stdout
    except Exception:
        # If the check itself fails, do not block the user; let the real
        # extraction produce the error.
        return True


def extract_audio(
    source: Path, destination: Path | None = None, ffmpeg: str = "ffmpeg"
) -> Path:
    """Write ``source``'s audio as 16 kHz mono WAV, the shape Whisper expects."""
    if not shutil.which(ffmpeg):
        raise AudioError(
            "FFmpeg is needed to read the audio out of your video.",
            reason="No “ffmpeg” executable was found on this computer.",
            suggestion="Install it with: brew install ffmpeg",
        )

    if destination is None:
        directory = Path(tempfile.mkdtemp(prefix="narration_transcribe_"))
        destination = directory / f"{source.stem}-16k.wav"
    destination.parent.mkdir(parents=True, exist_ok=True)

    command = [
        ffmpeg, "-y", "-loglevel", "error",
        "-i", str(source),
        "-vn",                       # drop video
        "-ac", "1",                  # mono
        "-ar", str(SAMPLE_RATE),     # 16 kHz
        "-c:a", "pcm_s16le",
        str(destination),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, timeout=1800)
    except subprocess.TimeoutExpired as exc:
        raise AudioError(
            "Reading the audio out of that file took too long and was stopped.",
            reason="FFmpeg did not finish within 30 minutes.",
            suggestion="Try a shorter video, or export the audio yourself first.",
            cause=exc,
        ) from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode("utf-8", "replace")[:2000]
        raise AudioError(
            "The audio could not be read out of that file.",
            reason="FFmpeg could not decode it, so the format may be unsupported.",
            suggestion="Try exporting the video again, or use a .mp4 or .mov file.",
            detail=detail,
            cause=exc,
        ) from exc

    if not destination.exists() or destination.stat().st_size == 0:
        raise AudioError(
            "That file does not seem to contain any audio.",
            reason="Reading it produced an empty audio track.",
            suggestion=(
                "If this is a silent screen recording, there is nothing to "
                "transcribe — write the script instead and import it as text."
            ),
        )
    return destination


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
