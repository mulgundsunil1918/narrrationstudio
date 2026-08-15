"""Application paths and settings (§25: no hard-coded user paths).

Every location is derived from the running user's home directory, following
macOS conventions, so the app works unchanged for any user. Nothing here writes
outside those directories.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

APP_NAME = "Narration Studio"
APP_ID = "NarrationStudio"
PROJECT_SUFFIX = ".narration"

# The proof-of-concept's output format, preserved exactly (§33).
DEFAULT_SAMPLE_RATE = 24_000
DEFAULT_CHANNELS = 1
DEFAULT_PEAK_TARGET = 0.92

DEFAULT_ENGINE = "kokoro"
DEFAULT_VOICE = "af_heart"
DEFAULT_LANG_CODE = "a"

VOICE_PREVIEW_TEXT = (
    "This is a preview of the selected voice, reading a short sample sentence "
    "so you can compare it with the others."
)


#: Redirects every per-user directory somewhere else. Set it to run against a
#: throwaway location — which the test suite does, so a test run can never read
#: or overwrite somebody's real projects, settings or recovery file.
DATA_DIR_ENV = "NARRATION_STUDIO_DATA_DIR"


def _base_dir(kind: str) -> Path:
    """Return a per-user directory for ``kind`` in ("support", "cache", "logs")."""
    override = os.environ.get(DATA_DIR_ENV)
    if override:
        return Path(override).expanduser() / kind

    from app.utils.platform import data_root

    return data_root(APP_NAME, APP_ID, kind)


def support_dir() -> Path:
    return _ensure(_base_dir("support"))


def cache_dir() -> Path:
    return _ensure(_base_dir("cache"))


def audio_cache_dir() -> Path:
    return _ensure(cache_dir() / "audio")


def preview_cache_dir() -> Path:
    return _ensure(cache_dir() / "voice-previews")


def log_dir() -> Path:
    return _ensure(_base_dir("logs"))


def autosave_dir() -> Path:
    return _ensure(support_dir() / "autosave")


def rules_path() -> Path:
    return support_dir() / "text-rules.json"


def settings_path() -> Path:
    return support_dir() / "settings.json"


def _ensure(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass
class Settings:
    """User preferences that survive between launches."""

    engine: str = DEFAULT_ENGINE
    voice: str = DEFAULT_VOICE
    lang_code: str = DEFAULT_LANG_CODE
    sample_rate: int = DEFAULT_SAMPLE_RATE
    last_import_dir: str = ""
    last_export_dir: str = ""
    recent_projects: list[str] = field(default_factory=list)
    favourite_voices: list[str] = field(default_factory=list)
    recent_voices: list[str] = field(default_factory=list)
    verbose_logging: bool = False
    window_geometry: str = ""

    @classmethod
    def load(cls) -> "Settings":
        path = settings_path()
        if not path.exists():
            return cls()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # Corrupt settings must never stop the app from starting.
            return cls()
        known = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in payload.items() if k in known})

    def save(self) -> None:
        path = settings_path()
        try:
            temporary = path.with_name(path.name + ".tmp")
            temporary.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
            temporary.replace(path)
        except OSError:
            pass  # preferences are a convenience, never a failure the user must see
