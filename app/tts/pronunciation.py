"""Pronunciation layer (§14).

Applied to the narration text on its way to the TTS engine and **nowhere else**.
Captions are never modified: a viewer still reads "NASA" and "API" while the
engine receives whatever spelling makes it say them correctly.

Two kinds of entry:

* ``spoken`` -- a respelling the engine reads naturally ("Acme" → "Ack-me").
* ``letters`` -- an initialism to be read letter by letter ("API" → "A P I").
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence

from app.core.errors import StudioError

_WORD_BOUNDARY = r"A-Za-z0-9"


@dataclass
class PronunciationEntry:
    """One term and how the engine should be told to say it."""

    term: str
    spoken: str
    case_sensitive: bool = False
    enabled: bool = True
    note: str = ""

    def compile(self) -> re.Pattern[str]:
        body = f"(?<![{_WORD_BOUNDARY}]){re.escape(self.term)}(?![{_WORD_BOUNDARY}])"
        flags = 0 if self.case_sensitive else re.IGNORECASE
        return re.compile(body, flags)


def spell_out(term: str) -> str:
    """Render an initialism so it is read letter by letter."""
    return " ".join(term.upper())


def default_entries() -> list[PronunciationEntry]:
    """No entries ship with the app.

    Which acronyms matter, and how they should be said, depends entirely on the
    subject of the script. Guessing on the user's behalf would mean the engine
    silently says something different from what the caption shows. Entries are
    added by the user in Settings, or imported from a file.
    """
    return []


def apply(text: str, entries: Sequence[PronunciationEntry]) -> str:
    """Rewrite ``text`` for the engine. Returns text; never touches captions."""
    result = text
    for entry in entries:
        if not entry.enabled or not entry.term:
            continue
        result = entry.compile().sub(entry.spoken.replace("\\", r"\\"), result)
    return result


def preview(text: str, entries: Sequence[PronunciationEntry]) -> list[tuple[str, str]]:
    """Return the (term, spoken) pairs that would actually fire for ``text``."""
    hits: list[tuple[str, str]] = []
    for entry in entries:
        if entry.enabled and entry.term and entry.compile().search(text):
            hits.append((entry.term, entry.spoken))
    return hits


def load(path: Path) -> list[PronunciationEntry]:
    if not path.exists():
        return default_entries()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise StudioError(
            "The pronunciation dictionary could not be read.",
            suggestion="Fix or delete it; the built-in entries will be used instead.",
            detail=str(path),
            cause=exc,
        ) from exc

    raw = payload.get("entries", payload) if isinstance(payload, dict) else payload
    if not isinstance(raw, list):
        return default_entries()
    known = {f.name for f in PronunciationEntry.__dataclass_fields__.values()}
    return [
        PronunciationEntry(**{k: v for k, v in item.items() if k in known})
        for item in raw
        if isinstance(item, dict) and "term" in item
    ]


def save(path: Path, entries: Sequence[PronunciationEntry]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "entries": [asdict(entry) for entry in entries]}
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temporary.replace(path)
    return path
