"""The TTS engine interface every backend implements.

Nothing above this layer knows what Kokoro is. Adding Piper, another local
model, or an explicitly-configured cloud service means writing one subclass and
registering it -- no changes to grouping, timing, assembly or the UI.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import numpy as np


class Locality(str, Enum):
    """Where audio is produced. Surfaced in the UI so it is never ambiguous."""

    LOCAL = "local"
    CLOUD = "cloud"

    @property
    def badge(self) -> str:
        return "● Local processing" if self is Locality.LOCAL else "☁ Cloud processing"


@dataclass(frozen=True)
class Voice:
    """A selectable voice.

    ``tags`` describe how the user intends to use the voice. They are UI
    metadata and are never presented as claims about what the model can do.
    """

    identifier: str
    name: str
    engine: str
    language: str = "English"
    lang_code: str = "a"
    gender: str = "Unspecified"
    tags: tuple[str, ...] = ()
    notes: str = ""

    @property
    def display(self) -> str:
        parts = [self.name]
        if self.gender != "Unspecified":
            parts.append(self.gender)
        return "  ·  ".join(parts)


@dataclass(frozen=True)
class GenerationRequest:
    text: str
    voice: str
    lang_code: str = "a"
    #: Engine-native speed control, when the model supports one. Distinct from
    #: the post-hoc time-stretch used to fit a window.
    speed: float = 1.0
    sample_rate: int = 24_000


@dataclass
class GenerationResult:
    """What an engine returns. ``audio`` is float32 mono in the range [-1, 1]."""

    audio: np.ndarray
    sample_rate: int
    duration_ms: int
    engine: str
    voice: str
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return self.audio is None or len(self.audio) == 0


class EngineUnavailable(Exception):
    """Raised when an engine cannot run, with a fixable reason."""

    def __init__(self, message: str, suggestion: str = "", detail: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.suggestion = suggestion
        self.detail = detail


class TTSEngine(ABC):
    """Base class for every speech backend."""

    identifier: str = "base"
    display_name: str = "Base engine"
    locality: Locality = Locality.LOCAL
    #: True when the engine honours ``GenerationRequest.speed`` itself. When
    #: False the app must not offer a speed control that silently does nothing.
    supports_speed: bool = False
    supports_pitch: bool = False

    @abstractmethod
    def is_available(self) -> tuple[bool, str]:
        """Return ``(available, reason)``. ``reason`` is empty when available."""

    @abstractmethod
    def voices(self) -> list[Voice]:
        """Voices this engine can currently use."""

    @abstractmethod
    def generate(self, request: GenerationRequest) -> GenerationResult:
        """Synthesise ``request.text``. Raises :class:`EngineUnavailable`."""

    def warm_up(self) -> None:
        """Optional: load models ahead of the first request."""

    def generate_to_file(self, request: GenerationRequest, path: Path) -> GenerationResult:
        """Synthesise and write a WAV, returning the same result."""
        import soundfile as sf

        result = self.generate(request)
        path.parent.mkdir(parents=True, exist_ok=True)
        sf.write(str(path), result.audio, result.sample_rate)
        return result
