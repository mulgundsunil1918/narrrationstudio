"""Engine and voice registry.

The one place that knows which engines exist. Adding a backend is a single
``register`` call.
"""

from __future__ import annotations

from typing import Callable, Iterable

from app.tts.base import TTSEngine, Voice
from app.tts.kokoro_engine import KokoroEngine

#: Voice categories offered in the voice library. A voice appears under a
#: heading because its tags say so, never because the app assumed it.
VOICE_CATEGORIES = (
    "Female", "Male", "Neutral", "Warm", "Bright", "Deep", "Calm",
    "Energetic", "Professional", "Narrator", "Medical / Educational",
)

_FACTORIES: dict[str, Callable[[], TTSEngine]] = {}
_INSTANCES: dict[str, TTSEngine] = {}


def register(identifier: str, factory: Callable[[], TTSEngine]) -> None:
    _FACTORIES[identifier] = factory


def engine(identifier: str) -> TTSEngine:
    """Return the singleton engine for ``identifier``, building it on demand."""
    if identifier not in _FACTORIES:
        raise KeyError(
            f"No speech engine named “{identifier}”. "
            f"Available: {', '.join(sorted(_FACTORIES)) or 'none'}"
        )
    if identifier not in _INSTANCES:
        _INSTANCES[identifier] = _FACTORIES[identifier]()
    return _INSTANCES[identifier]


def engine_ids() -> list[str]:
    return sorted(_FACTORIES)


def engines() -> list[TTSEngine]:
    return [engine(identifier) for identifier in engine_ids()]


def all_voices() -> list[Voice]:
    voices: list[Voice] = []
    for backend in engines():
        available, _ = backend.is_available()
        if available:
            voices.extend(backend.voices())
    return voices


def find_voice(identifier: str) -> tuple[TTSEngine, Voice] | None:
    """Locate a voice across every registered engine."""
    for backend in engines():
        for voice in backend.voices():
            if voice.identifier == identifier:
                return backend, voice
    return None


def group_by_category(voices: Iterable[Voice]) -> dict[str, list[Voice]]:
    """Bucket voices under every category their tags and gender place them in."""
    grouped: dict[str, list[Voice]] = {name: [] for name in VOICE_CATEGORIES}
    for voice in voices:
        if voice.gender in grouped:
            grouped[voice.gender].append(voice)
        for tag in voice.tags:
            if tag in grouped:
                grouped[tag].append(voice)
    return {name: found for name, found in grouped.items() if found}


register("kokoro", KokoroEngine)
