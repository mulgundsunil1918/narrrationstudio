"""Import subtitles from .srt, and plain text from .txt / .md.

The SRT reader is deliberately tolerant: real-world files (including the ones
Whisper produces) have missing indices, stray blank lines, CRLF endings, a BOM,
and occasionally ``.`` instead of ``,`` in timestamps. Rather than refusing the
file, malformed blocks are skipped and reported as warnings so the user can see
exactly what was dropped.

Timestamps are never adjusted on import -- what the file says is what the
document gets (§2, §43).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from app.core.errors import FileFormatError, UnsupportedFileError
from app.core.models import Segment
from app.core.timecode import TimecodeError, parse_timestamp

SUPPORTED_SUBTITLE_SUFFIXES = {".srt"}
SUPPORTED_TEXT_SUFFIXES = {".txt", ".md", ".markdown"}
SUPPORTED_SUFFIXES = SUPPORTED_SUBTITLE_SUFFIXES | SUPPORTED_TEXT_SUFFIXES

# Reading pace used only when synthesising timings for plain text, which has
# none of its own. Roughly 150 wpm, a comfortable narration speed.
DEFAULT_CHARS_PER_SECOND = 15.0
DEFAULT_GAP_MS = 250
MIN_TEXT_SEGMENT_MS = 1200

_ARROW_RE = re.compile(r"(.+?)\s*-{2,}>\s*(.+)")
_INDEX_RE = re.compile(r"^\d+$")
# Files larger than this are almost certainly not subtitles (§39: validate input).
MAX_IMPORT_BYTES = 32 * 1024 * 1024


@dataclass
class ParseResult:
    segments: list[Segment]
    warnings: list[str] = field(default_factory=list)
    source_format: str = "srt"
    path: Path | None = None

    @property
    def count(self) -> int:
        return len(self.segments)


def read_text_file(path: Path) -> str:
    """Read a file as text, tolerating the encodings subtitle files show up in."""
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise FileFormatError(
            f"Could not open “{path.name}”.",
            reason="The file could not be found, or the system refused to read it.",
            suggestion="Check that the file still exists and is readable.",
            cause=exc,
        ) from exc

    if size > MAX_IMPORT_BYTES:
        raise FileFormatError(
            f"“{path.name}” is too large to be a subtitle file.",
            reason=f"It is {size / 1_048_576:.0f} MB; subtitles are usually well under 1 MB.",
            suggestion="Check that you selected the subtitle file and not a media file.",
        )

    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise FileFormatError(
        f"“{path.name}” does not appear to be a text file.",
        reason="Its contents could not be decoded as text in any common encoding.",
        suggestion="Re-export the subtitles as UTF-8 encoded SRT.",
    )


def load(path: Path) -> ParseResult:
    """Import any supported file, dispatching on its extension."""
    suffix = path.suffix.lower()
    if suffix in SUPPORTED_SUBTITLE_SUFFIXES:
        return parse_srt(read_text_file(path), path=path)
    if suffix in SUPPORTED_TEXT_SUFFIXES:
        return parse_plain_text(read_text_file(path), path=path)
    raise UnsupportedFileError(
        f"“{path.name}” cannot be imported.",
        reason=f"“{suffix or 'A file with no extension'}” is not a subtitle or text format.",
        suggestion="Import an .srt subtitle file, or a .txt / .md text file.",
    )


def parse_srt(content: str, path: Path | None = None) -> ParseResult:
    """Parse SRT content into segments, collecting warnings for bad blocks."""
    warnings: list[str] = []
    segments: list[Segment] = []

    normalised = content.replace("\r\n", "\n").replace("\r", "\n").strip("﻿\n ")
    if not normalised.strip():
        raise FileFormatError(
            "This subtitle file is empty.",
            reason="The file contains no text at all.",
            suggestion="Choose a file that contains subtitles.",
        )

    blocks = re.split(r"\n\s*\n+", normalised)
    for block_number, block in enumerate(blocks, start=1):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines:
            continue

        # The index line is optional; find the timing line wherever it is.
        timing_line = next(
            (i for i, line in enumerate(lines) if _ARROW_RE.search(line)), None
        )
        if timing_line is None:
            warnings.append(
                f"Block {block_number}: no “-->” timing line, skipped."
            )
            continue

        match = _ARROW_RE.search(lines[timing_line])
        assert match is not None
        try:
            # Trailing position data ("X1:.. Y1:..") is legal in SRT; ignore it.
            start_ms = parse_timestamp(match.group(1).split()[-1])
            end_ms = parse_timestamp(match.group(2).split()[0])
        except (TimecodeError, IndexError):
            warnings.append(
                f"Block {block_number}: could not read the timestamps "
                f"({lines[timing_line]}), skipped."
            )
            continue

        text_lines = lines[timing_line + 1 :]
        # Anything before the timing line that is not a bare index is stray text.
        for stray in lines[:timing_line]:
            if not _INDEX_RE.match(stray):
                warnings.append(
                    f"Block {block_number}: ignored unexpected line “{stray}”."
                )

        text = " ".join(text_lines).strip()
        if not text:
            warnings.append(
                f"Block {block_number}: has timings but no text; imported as silent."
            )

        if end_ms < start_ms:
            warnings.append(
                f"Block {block_number}: ends before it starts; timings swapped."
            )
            start_ms, end_ms = end_ms, start_ms

        segments.append(
            Segment(start_ms=start_ms, end_ms=end_ms, text=text, source_text=text)
        )

    if not segments:
        raise FileFormatError(
            "No subtitles could be read from this file.",
            reason=(
                f"{len(blocks)} block(s) were examined and none contained a valid "
                "“start --> end” timing line."
            ),
            suggestion=(
                "Check that it is a standard SRT file with lines like "
                "“00:00:00,000 --> 00:00:04,680”."
            ),
            detail="\n".join(warnings),
        )

    return ParseResult(
        segments=segments, warnings=warnings, source_format="srt", path=path
    )


def parse_plain_text(
    content: str,
    path: Path | None = None,
    chars_per_second: float = DEFAULT_CHARS_PER_SECOND,
    gap_ms: int = DEFAULT_GAP_MS,
    start_ms: int = 0,
) -> ParseResult:
    """Turn unstructured text into timed segments.

    Plain text carries no timing, so a timeline is estimated from reading pace.
    This is the one import path that invents timestamps, and the warning makes
    that explicit -- the user is expected to adjust them.
    """
    sentences = _split_sentences(_strip_markdown(content))
    if not sentences:
        raise FileFormatError(
            "This file contains no readable text.",
            reason="After removing formatting, no sentences remained.",
            suggestion="Choose a file with narration text in it.",
        )

    segments: list[Segment] = []
    cursor = start_ms
    for sentence in sentences:
        estimated = int(round(len(sentence) / chars_per_second * 1000))
        duration = max(MIN_TEXT_SEGMENT_MS, estimated)
        segments.append(
            Segment(
                start_ms=cursor,
                end_ms=cursor + duration,
                text=sentence,
                source_text=sentence,
            )
        )
        cursor += duration + gap_ms

    warnings = [
        f"Timings were estimated from reading pace (~{chars_per_second:.0f} "
        "characters per second) because plain text has none. Adjust them before "
        "generating if the narration must match a video."
    ]
    return ParseResult(
        segments=segments,
        warnings=warnings,
        source_format=path.suffix.lower().lstrip(".") if path else "txt",
        path=path,
    )


def _strip_markdown(text: str) -> str:
    """Remove the markdown syntax that should not be spoken aloud."""
    text = re.sub(r"^```.*?^```", "", text, flags=re.MULTILINE | re.DOTALL)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)          # images
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)      # links -> label
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s{0,3}>\s?", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*[-*+]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"(\*\*|__|\*|_)", "", text)
    text = re.sub(r"^\s*[-*_]{3,}\s*$", "", text, flags=re.MULTILINE)
    return text


def _split_sentences(text: str) -> list[str]:
    """Split into sentences, keeping abbreviations like “Dr.” intact."""
    collapsed = re.sub(r"\s+", " ", text).strip()
    if not collapsed:
        return []
    # Split after . ? ! followed by whitespace + a capital/digit, unless the
    # token before the stop is a known abbreviation.
    parts = re.split(r"(?<=[.!?])\s+(?=[\"'“(]?[A-Z0-9])", collapsed)

    merged: list[str] = []
    for part in parts:
        # A split is only real if the preceding sentence did not end on an
        # abbreviation ("Ask Dr. | Rao about the dose." is one sentence).
        if merged and (_ends_with_abbreviation(merged[-1]) or len(part) < 3):
            merged[-1] = f"{merged[-1]} {part}"
        else:
            merged.append(part)
    return [p.strip() for p in merged if p.strip()]


# Deliberately excludes ambiguous words like "no" and "co" that are far more
# often ordinary sentence endings than abbreviations.
_ABBREVIATIONS = {
    "dr", "mr", "mrs", "ms", "prof", "st", "vs", "etc", "e.g", "i.e",
    "fig", "approx", "inc", "ltd", "dept", "jr", "sr", "al",
}


def _ends_with_abbreviation(text: str) -> bool:
    if not text.endswith("."):
        return False
    last = text.rsplit(" ", 1)[-1].rstrip(".").lower()
    # A single initial ("J.") is an abbreviation too.
    return last in _ABBREVIATIONS or (len(last) == 1 and last.isalpha())
