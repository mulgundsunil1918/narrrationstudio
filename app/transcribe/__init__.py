"""Speech-to-text: turning a video that already has a voice on it into captions.

Importing the package registers the bundled engine, mirroring ``app.tts``.
"""

from __future__ import annotations

from app.transcribe.base import (  # noqa: F401
    AUDIO_SUFFIXES,
    MEDIA_SUFFIXES,
    VIDEO_SUFFIXES,
    TranscribeRequest,
    Transcriber,
    TranscriptionResult,
    Utterance,
    extract_audio,
    has_audio_track,
    media_duration_ms,
    to_segments,
    transcriber,
    transcriber_ids,
)
from app.transcribe import whisper_engine  # noqa: F401  (registers "whisper")

__all__ = [
    "AUDIO_SUFFIXES",
    "MEDIA_SUFFIXES",
    "VIDEO_SUFFIXES",
    "TranscribeRequest",
    "Transcriber",
    "TranscriptionResult",
    "Utterance",
    "extract_audio",
    "has_audio_track",
    "media_duration_ms",
    "to_segments",
    "transcriber",
    "transcriber_ids",
]
