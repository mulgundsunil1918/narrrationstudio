"""Timeline validation (§12).

Runs over the subtitle document and reports anything that would produce wrong or
misaligned audio. Every issue carries a plain-language message and a concrete
suggested action -- never a stack trace (§27).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Sequence

from app.core.models import Segment, SegmentStatus
from app.core.timecode import format_display, format_duration

# A subtitle shorter than this is very unlikely to hold intelligible speech.
VERY_SHORT_MS = 400
# Characters per second above which the text almost certainly cannot be read in
# the available window at a natural pace. Kokoro at 1.0x averages ~15 cps.
DENSE_CPS = 22.0


class Severity(str, Enum):
    ERROR = "error"      # will produce wrong audio; must be fixed
    WARNING = "warning"  # will produce audio, but quality is at risk
    INFO = "info"        # worth knowing, not a problem


@dataclass(frozen=True)
class Issue:
    severity: Severity
    code: str
    message: str
    suggestion: str
    segment_index: int | None = None   # 0-based
    related_index: int | None = None

    @property
    def display_number(self) -> str:
        if self.segment_index is None:
            return "—"
        return str(self.segment_index + 1)


@dataclass(frozen=True)
class TimelineReport:
    issues: tuple[Issue, ...]
    segment_count: int
    timeline_end_ms: int
    total_speech_ms: int
    gap_count: int
    gap_ms: int

    @property
    def errors(self) -> tuple[Issue, ...]:
        return tuple(i for i in self.issues if i.severity is Severity.ERROR)

    @property
    def warnings(self) -> tuple[Issue, ...]:
        return tuple(i for i in self.issues if i.severity is Severity.WARNING)

    @property
    def infos(self) -> tuple[Issue, ...]:
        return tuple(i for i in self.issues if i.severity is Severity.INFO)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def flagged_indices(self) -> tuple[int, ...]:
        seen: list[int] = []
        for issue in self.issues:
            if issue.severity is Severity.INFO:
                continue
            if issue.segment_index is not None and issue.segment_index not in seen:
                seen.append(issue.segment_index)
        return tuple(seen)

    def summary(self) -> str:
        """The one-line status shown at the bottom of the window (§12)."""
        if not self.segment_count:
            return "No subtitles loaded"
        flagged = len(self.flagged_indices)
        if not flagged:
            return f"✓ {self.segment_count}/{self.segment_count} segments synchronized"
        word = "segment needs" if flagged == 1 else "segments need"
        return f"⚠ {flagged} {word} attention"


def validate(segments: Sequence[Segment]) -> TimelineReport:
    """Check a subtitle list for anything that breaks timeline integrity."""
    issues: list[Issue] = []
    ordered = sorted(range(len(segments)), key=lambda i: segments[i].start_ms)

    gap_count = 0
    gap_ms = 0

    for position, index in enumerate(ordered):
        segment = segments[index]
        issues.extend(_check_single(index, segment))

        if position == 0:
            continue
        previous_index = ordered[position - 1]
        previous = segments[previous_index]

        if segment.start_ms < previous.end_ms:
            overlap = previous.end_ms - segment.start_ms
            issues.append(
                Issue(
                    severity=Severity.ERROR,
                    code="overlap",
                    message=(
                        f"Subtitle {index + 1} starts {format_duration(overlap)} "
                        f"before subtitle {previous_index + 1} ends."
                    ),
                    suggestion=(
                        f"Move the start of subtitle {index + 1} to "
                        f"{format_display(previous.end_ms)} or later, or shorten "
                        f"subtitle {previous_index + 1}."
                    ),
                    segment_index=index,
                    related_index=previous_index,
                )
            )
        elif segment.start_ms > previous.end_ms:
            gap_count += 1
            gap_ms += segment.start_ms - previous.end_ms

    if len(segments) > 1 and ordered != list(range(len(segments))):
        issues.append(
            Issue(
                severity=Severity.WARNING,
                code="out_of_order",
                message="Subtitles are not listed in chronological order.",
                suggestion="Use Edit ▸ Sort by Start Time to reorder them.",
            )
        )

    timeline_end = max((s.end_ms for s in segments), default=0)
    return TimelineReport(
        issues=tuple(issues),
        segment_count=len(segments),
        timeline_end_ms=timeline_end,
        total_speech_ms=sum(s.duration_ms for s in segments),
        gap_count=gap_count,
        gap_ms=gap_ms,
    )


def _check_single(index: int, segment: Segment) -> list[Issue]:
    """Checks that depend only on one segment."""
    issues: list[Issue] = []
    number = index + 1

    if segment.start_ms < 0:
        issues.append(
            Issue(
                Severity.ERROR,
                "negative_start",
                f"Subtitle {number} starts before the beginning of the timeline.",
                "Set its start time to 00:00:00.000 or later.",
                index,
            )
        )

    if segment.duration_ms <= 0:
        issues.append(
            Issue(
                Severity.ERROR,
                "non_positive_duration",
                f"Subtitle {number} ends at or before it starts.",
                "Set the end time later than the start time.",
                index,
            )
        )
    elif segment.duration_ms < VERY_SHORT_MS:
        issues.append(
            Issue(
                Severity.WARNING,
                "very_short",
                f"Subtitle {number} is only {format_duration(segment.duration_ms)} long.",
                "Lengthen the window or merge it with a neighbour.",
                index,
            )
        )

    text = segment.text.strip()
    if not text:
        issues.append(
            Issue(
                Severity.WARNING,
                "empty_text",
                f"Subtitle {number} has no text, so it will render as silence.",
                "Add text, or delete the subtitle to leave an intentional gap.",
                index,
            )
        )
    elif segment.duration_ms > 0:
        cps = len(text) / (segment.duration_ms / 1000)
        if cps > DENSE_CPS:
            issues.append(
                Issue(
                    Severity.WARNING,
                    "dense_text",
                    (
                        f"Subtitle {number} packs {len(text)} characters into "
                        f"{format_duration(segment.duration_ms)} "
                        f"({cps:.0f} chars/sec) and will likely need speeding up."
                    ),
                    "Shorten the text, lengthen the window, or split the subtitle.",
                    index,
                )
            )

    if segment.status is SegmentStatus.FAILED:
        issues.append(
            Issue(
                Severity.ERROR,
                "generation_failed",
                f"Subtitle {number} failed to generate: {segment.error or 'unknown reason'}",
                "Retry generation, or choose a different voice.",
                index,
            )
        )
    elif segment.status is SegmentStatus.NEEDS_REGEN:
        issues.append(
            Issue(
                Severity.WARNING,
                "stale_audio",
                f"Subtitle {number} was edited after its audio was generated.",
                "Regenerate this subtitle before exporting.",
                index,
            )
        )

    return issues
