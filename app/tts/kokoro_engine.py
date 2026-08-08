"""Kokoro backend — the first :class:`TTSEngine` implementation.

Wraps ``KPipeline`` exactly as the proven proof-of-concept used it: one pipeline
per language code, chunks concatenated in order, float32 mono at 24 kHz. The
model is loaded lazily so importing this module never pulls torch into a process
that only wanted to parse subtitles.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

import numpy as np

from app.tts.base import (
    EngineUnavailable,
    GenerationRequest,
    GenerationResult,
    Locality,
    TTSEngine,
    Voice,
)

logger = logging.getLogger(__name__)

SAMPLE_RATE = 24_000

#: Voices shipped in the Kokoro-82M repository. ``tags`` are how the voice is
#: useful in this app, not claims about model capability -- Kokoro has no
#: emotion control, and the app must not imply otherwise.
KOKORO_VOICES: tuple[dict, ...] = (
    {"identifier": "af_heart", "name": "Heart", "gender": "Female",
     "tags": ("Warm", "Narrator", "Medical / Educational"),
     "notes": "A good default for narration."},
    {"identifier": "af_bella", "name": "Bella", "gender": "Female",
     "tags": ("Warm", "Calm")},
    {"identifier": "af_nicole", "name": "Nicole", "gender": "Female",
     "tags": ("Calm", "Narrator")},
    {"identifier": "af_sarah", "name": "Sarah", "gender": "Female",
     "tags": ("Professional",)},
    {"identifier": "af_sky", "name": "Sky", "gender": "Female",
     "tags": ("Bright", "Energetic")},
    {"identifier": "af_alloy", "name": "Alloy", "gender": "Female",
     "tags": ("Neutral", "Professional")},
    {"identifier": "af_aoede", "name": "Aoede", "gender": "Female",
     "tags": ("Warm",)},
    {"identifier": "af_jessica", "name": "Jessica", "gender": "Female",
     "tags": ("Professional",)},
    {"identifier": "af_kore", "name": "Kore", "gender": "Female",
     "tags": ("Calm",)},
    {"identifier": "af_nova", "name": "Nova", "gender": "Female",
     "tags": ("Bright",)},
    {"identifier": "af_river", "name": "River", "gender": "Female",
     "tags": ("Calm",)},
    {"identifier": "am_adam", "name": "Adam", "gender": "Male",
     "tags": ("Narrator",)},
    {"identifier": "am_michael", "name": "Michael", "gender": "Male",
     "tags": ("Professional", "Narrator")},
    {"identifier": "am_echo", "name": "Echo", "gender": "Male",
     "tags": ("Neutral",)},
    {"identifier": "am_eric", "name": "Eric", "gender": "Male",
     "tags": ("Bright",)},
    {"identifier": "am_fenrir", "name": "Fenrir", "gender": "Male",
     "tags": ("Deep",)},
    {"identifier": "am_liam", "name": "Liam", "gender": "Male",
     "tags": ("Calm",)},
    {"identifier": "am_onyx", "name": "Onyx", "gender": "Male",
     "tags": ("Deep", "Narrator")},
    {"identifier": "am_puck", "name": "Puck", "gender": "Male",
     "tags": ("Energetic",)},
    {"identifier": "bf_emma", "name": "Emma", "gender": "Female",
     "tags": ("Narrator",), "lang_code": "b", "language": "English (British)"},
    {"identifier": "bf_isabella", "name": "Isabella", "gender": "Female",
     "tags": ("Professional",), "lang_code": "b", "language": "English (British)"},
    {"identifier": "bm_george", "name": "George", "gender": "Male",
     "tags": ("Narrator",), "lang_code": "b", "language": "English (British)"},
    {"identifier": "bm_lewis", "name": "Lewis", "gender": "Male",
     "tags": ("Deep",), "lang_code": "b", "language": "English (British)"},
)

HF_REPO = "hexgrad/Kokoro-82M"


class KokoroEngine(TTSEngine):
    identifier = "kokoro"
    display_name = "Kokoro"
    locality = Locality.LOCAL
    #: KPipeline takes a ``speed`` argument, so this is a real control.
    supports_speed = True
    #: Kokoro exposes no pitch parameter. Claiming one would be a fake control.
    supports_pitch = False

    def __init__(self) -> None:
        self._pipelines: dict[str, object] = {}
        # Pipelines are built lazily and shared. Without a lock, two threads
        # asking at once each build their own -- several seconds and roughly a
        # gigabyte of duplicated model, for no benefit.
        self._lock = threading.RLock()

    # -- availability ----------------------------------------------------

    def is_available(self) -> tuple[bool, str]:
        try:
            import kokoro  # noqa: F401
        except ImportError:
            return False, (
                "The Kokoro package is not installed in this environment."
            )
        try:
            import torch  # noqa: F401
        except ImportError:
            return False, "PyTorch is not installed, and Kokoro needs it to run."
        return True, ""

    def installed_voice_files(self) -> set[str]:
        """Voice identifiers already downloaded, so the UI can flag the rest.

        Reads the Hugging Face cache directly rather than reaching the network,
        keeping start-up offline.
        """
        found: set[str] = set()
        try:
            from huggingface_hub import constants

            root = Path(constants.HF_HUB_CACHE)
        except Exception:
            root = Path.home() / ".cache" / "huggingface" / "hub"

        repo_dir = root / f"models--{HF_REPO.replace('/', '--')}"
        if not repo_dir.exists():
            return found
        for path in repo_dir.glob("snapshots/*/voices/*.pt"):
            found.add(path.stem)
        return found

    # -- voices ----------------------------------------------------------

    def voices(self) -> list[Voice]:
        return [
            Voice(
                identifier=entry["identifier"],
                name=entry["name"],
                engine=self.identifier,
                language=entry.get("language", "English (US)"),
                lang_code=entry.get("lang_code", "a"),
                gender=entry.get("gender", "Unspecified"),
                tags=tuple(entry.get("tags", ())),
                notes=entry.get("notes", ""),
            )
            for entry in KOKORO_VOICES
        ]

    def voice(self, identifier: str) -> Voice | None:
        return next((v for v in self.voices() if v.identifier == identifier), None)

    # -- generation ------------------------------------------------------

    def _pipeline(self, lang_code: str):
        available, reason = self.is_available()
        if not available:
            raise EngineUnavailable(
                reason,
                suggestion="Run ./setup.sh to install the local speech engine.",
            )
        with self._lock:
            if lang_code not in self._pipelines:
                from kokoro import KPipeline

                logger.info("Loading Kokoro pipeline for lang_code=%s", lang_code)
                started = time.monotonic()
                try:
                    self._pipelines[lang_code] = KPipeline(lang_code=lang_code)
                except Exception as exc:
                    raise EngineUnavailable(
                        "The Kokoro voice model could not be loaded.",
                        suggestion=(
                            "Check your internet connection for the first-time model "
                            "download, then try again. Later runs work offline."
                        ),
                        detail=str(exc),
                    ) from exc
                logger.info("Kokoro ready in %.1fs", time.monotonic() - started)
            return self._pipelines[lang_code]

    def warm_up(self, lang_code: str = "a") -> None:
        self._pipeline(lang_code)

    def generate(self, request: GenerationRequest) -> GenerationResult:
        if not request.text.strip():
            return GenerationResult(
                audio=np.zeros(0, dtype=np.float32),
                sample_rate=SAMPLE_RATE,
                duration_ms=0,
                engine=self.identifier,
                voice=request.voice,
            )

        pipeline = self._pipeline(request.lang_code)
        started = time.monotonic()
        try:
            # KPipeline yields (graphemes, phonemes, audio) per internal chunk.
            # Concatenating in order is what the proven pipeline did; the chunks
            # are consecutive parts of one utterance, so no gap is inserted.
            chunks = [
                np.asarray(audio, dtype=np.float32)
                for _, _, audio in pipeline(
                    request.text, voice=request.voice, speed=request.speed
                )
            ]
        except Exception as exc:
            raise EngineUnavailable(
                f"Kokoro could not speak this text with the voice “{request.voice}”.",
                suggestion=(
                    "Try another voice, or shorten the text if it contains unusual "
                    "characters."
                ),
                detail=str(exc),
            ) from exc

        if not chunks:
            return GenerationResult(
                audio=np.zeros(0, dtype=np.float32),
                sample_rate=SAMPLE_RATE,
                duration_ms=0,
                engine=self.identifier,
                voice=request.voice,
            )

        audio = np.concatenate(chunks).astype(np.float32, copy=False)
        elapsed = time.monotonic() - started
        duration_ms = int(round(len(audio) / SAMPLE_RATE * 1000))
        logger.info(
            "Generated %.2fs of audio in %.2fs (voice=%s, chunks=%d)",
            duration_ms / 1000,
            elapsed,
            request.voice,
            len(chunks),
        )
        return GenerationResult(
            audio=audio,
            sample_rate=SAMPLE_RATE,
            duration_ms=duration_ms,
            engine=self.identifier,
            voice=request.voice,
            metadata={
                "chunks": str(len(chunks)),
                "generation_seconds": f"{elapsed:.2f}",
                "speed": f"{request.speed:.2f}",
            },
        )
