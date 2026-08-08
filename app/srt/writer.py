"""Export subtitles back to SRT (§20).

Timestamps are written exactly as they are held in the document. The document
only ever holds a timestamp the file supplied or the user typed, so a round-trip
import → export is lossless for timing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from app.core.models import Segment
from app.core.timecode import format_timestamp

# LF keeps diffs clean and every subtitle player accepts it.
LINE_ENDING = "\n"


def to_srt(segments: Sequence[Segment]) -> str:
    """Render segments as SRT text, numbered sequentially in time order."""
    blocks: list[str] = []
    ordered = sorted(segments, key=lambda s: (s.start_ms, s.end_ms))
    for number, segment in enumerate(ordered, start=1):
        text = segment.text.strip() or " "
        blocks.append(
            f"{number}{LINE_ENDING}"
            f"{format_timestamp(segment.start_ms)} --> "
            f"{format_timestamp(segment.end_ms)}{LINE_ENDING}"
            f"{text}"
        )
    return (LINE_ENDING * 2).join(blocks) + LINE_ENDING


def write_srt(path: Path, segments: Sequence[Segment]) -> Path:
    """Write segments to ``path`` atomically, so a failure cannot truncate it."""
    content = to_srt(segments)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)
    return path
