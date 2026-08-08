"""Content-addressed cache for generated narration groups (§ Cache).

A group's audio is keyed by everything that affects the waveform. Change the
text, the voice, the engine or the speed and the key changes; change a caption
in a different group and it does not, so unrelated work is never redone.

Deliberately excluded from the key: the group's *window*. Fitting happens after
generation, so the same speech can be refitted to a new window without asking
the engine for it again.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

CACHE_VERSION = 1


@dataclass(frozen=True)
class CacheKey:
    engine: str
    model: str
    voice: str
    lang_code: str
    text: str
    speed: float
    sample_rate: int
    extra: str = ""

    def digest(self) -> str:
        payload = json.dumps(
            {
                "version": CACHE_VERSION,
                "engine": self.engine,
                "model": self.model,
                "voice": self.voice,
                "lang": self.lang_code,
                "text": self.text,
                "speed": round(float(self.speed), 4),
                "rate": self.sample_rate,
                "extra": self.extra,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


class AudioCache:
    """Stores rendered group audio as WAV files under a hashed name."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.hits = 0
        self.misses = 0

    def path_for(self, key: CacheKey) -> Path:
        return self.directory / f"{key.digest()}.wav"

    def get(self, key: CacheKey) -> tuple[np.ndarray, int] | None:
        path = self.path_for(key)
        if not path.exists():
            self.misses += 1
            return None
        try:
            import soundfile as sf

            audio, rate = sf.read(str(path), dtype="float32", always_2d=False)
        except Exception as exc:
            # A corrupt cache entry must never break generation.
            logger.warning("Discarding unreadable cache entry %s: %s", path.name, exc)
            path.unlink(missing_ok=True)
            self.misses += 1
            return None
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        self.hits += 1
        return audio.astype(np.float32, copy=False), int(rate)

    def put(self, key: CacheKey, audio: np.ndarray, sample_rate: int) -> Path:
        import soundfile as sf

        path = self.path_for(key)
        temporary = path.with_suffix(".tmp.wav")
        sf.write(str(temporary), audio, sample_rate)
        temporary.replace(path)
        return path

    def size_bytes(self) -> int:
        return sum(f.stat().st_size for f in self.directory.glob("*.wav"))

    def clear(self) -> int:
        count = 0
        for path in self.directory.glob("*.wav"):
            path.unlink(missing_ok=True)
            count += 1
        return count

    def prune_to(self, max_bytes: int) -> int:
        """Delete the least recently used entries until under ``max_bytes``."""
        files = sorted(
            self.directory.glob("*.wav"), key=lambda p: p.stat().st_atime
        )
        total = sum(f.stat().st_size for f in files)
        removed = 0
        for path in files:
            if total <= max_bytes:
                break
            total -= path.stat().st_size
            path.unlink(missing_ok=True)
            removed += 1
        return removed

    @property
    def summary(self) -> str:
        return f"{self.hits} cached, {self.misses} generated"


def copy_into(path: Path, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, destination)
    return destination
