"""Human-readable validation of a narration plan, before and after generation.

Pre-generation answers "are these groups sensible?"; post-generation answers
"did the audio actually land where it should?". Both return plain data so the
CLI can print a table and the UI can render the same facts as a list.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from app.audio.timing import FitPlan
from app.core.models import Segment
from app.core.timecode import format_display
from app.narration.grouping import _ends_sentence, _last_word
from app.narration.groups import GroupWindow, NarrationPlan, SpeedSafety

#: Words that make a group ending look like an unfinished thought.
CONTINUATION_WORDS = {
    "and", "or", "but", "because", "which", "that", "to", "for", "with",
    "from", "of", "in", "on", "as", "including", "such", "than", "the", "a",
    "an", "at", "by", "into", "over", "under", "across", "through", "is",
    "are", "was", "were", "has", "have", "had", "will", "can", "these", "this",
}


@dataclass(frozen=True)
class GroupPreview:
    """One row of the pre-generation table."""

    number: int
    start_ms: int
    end_ms: int
    target_ms: int
    caption_span: tuple[int, int]
    text: str
    natural_boundary: bool
    ends_on: str
    forced_cut: bool
    gap_before_ms: int

    @property
    def duration_s(self) -> float:
        return self.target_ms / 1000

    @property
    def boundary_label(self) -> str:
        return "YES" if self.natural_boundary else "NO"

    @property
    def warning(self) -> str:
        if self.natural_boundary:
            return ""
        return (
            f"ends on “{self.ends_on}”, which reads as an unfinished phrase — "
            "the next group continues the same sentence"
        )


def preview_plan(plan: NarrationPlan, segments: Sequence[Segment]) -> list[GroupPreview]:
    """Describe every group before any audio is generated (§ pre-validation)."""
    window = GroupWindow(segments)
    rows: list[GroupPreview] = []
    previous_end: int | None = None

    for number, group in enumerate(plan, start=1):
        indices = window.indices_of(group)
        if not indices:
            continue
        start = window.start_ms(group)
        end = window.end_ms(group)
        text = window.narration_text(group)
        last = _last_word(text)
        natural = _ends_sentence(text) or last not in CONTINUATION_WORDS

        rows.append(
            GroupPreview(
                number=number,
                start_ms=start,
                end_ms=end,
                target_ms=end - start,
                caption_span=(indices[0] + 1, indices[-1] + 1),
                text=text,
                natural_boundary=natural,
                ends_on=last,
                forced_cut=group.forced_cut,
                gap_before_ms=0 if previous_end is None else start - previous_end,
            )
        )
        previous_end = end
    return rows


def format_preview_table(rows: Sequence[GroupPreview], width: int = 78) -> str:
    """Render the pre-generation table (§9)."""
    lines = [
        f"{'GROUP':<6}{'START':>14}{'END':>14}{'DURATION':>10}{'CAPTIONS':>10}  NATURAL?",
        "-" * width,
    ]
    for row in rows:
        span = f"{row.caption_span[0]}-{row.caption_span[1]}"
        lines.append(
            f"{row.number:<6}{format_display(row.start_ms):>14}"
            f"{format_display(row.end_ms):>14}{row.duration_s:>9.2f}s{span:>10}"
            f"  {row.boundary_label}"
        )
        preview = row.text if len(row.text) <= width - 8 else row.text[: width - 11] + "…"
        lines.append(f'       "{preview}"')
        if row.warning:
            lines.append(f"       ! {row.warning}")
        if row.gap_before_ms > 0:
            lines.append(
                f"       · {row.gap_before_ms / 1000:.2f}s gap before this group "
                "comes from the SRT and is preserved"
            )
    return "\n".join(lines)


@dataclass(frozen=True)
class GroupOutcome:
    """One row of the post-generation table."""

    number: int
    target_ms: int
    natural_ms: int
    final_ms: int
    speed_factor: float
    silence_ms: int
    safety: SpeedSafety
    unnatural_silence: bool

    @property
    def adjustment_percent(self) -> float:
        """Positive means the speech was slowed down to fill its window."""
        if self.natural_ms <= 0:
            return 0.0
        return (self.final_ms - self.natural_ms - self.silence_ms) / self.natural_ms * 100


def outcomes_from(plans: Sequence[FitPlan]) -> list[GroupOutcome]:
    return [
        GroupOutcome(
            number=index + 1,
            target_ms=plan.target_ms,
            natural_ms=plan.generated_ms,
            final_ms=plan.final_ms,
            speed_factor=plan.speed_factor,
            silence_ms=plan.silence_inserted_ms,
            safety=plan.safety,
            unnatural_silence=plan.unnatural_silence,
        )
        for index, plan in enumerate(plans)
    ]


def format_outcome_table(rows: Sequence[GroupOutcome], width: int = 78) -> str:
    """Render the post-generation table (§10)."""
    lines = [
        f"{'GROUP':<6}{'TARGET':>10}{'NATURAL':>10}{'FINAL':>10}"
        f"{'ADJUST':>9}{'SILENCE':>10}",
        "-" * width,
    ]
    for row in rows:
        mark = "  <-- unnatural internal silence" if row.unnatural_silence else ""
        lines.append(
            f"{row.number:<6}{row.target_ms / 1000:>9.2f}s{row.natural_ms / 1000:>9.2f}s"
            f"{row.final_ms / 1000:>9.2f}s{row.adjustment_percent:>+8.1f}%"
            f"{row.silence_ms / 1000:>9.2f}s{mark}"
        )
    return "\n".join(lines)


def silence_warnings(rows: Sequence[GroupOutcome]) -> list[str]:
    """Groups whose padding is large enough to be heard as a hole."""
    return [
        f"Group {row.number}: {row.silence_ms / 1000:.2f}s of silence was inserted "
        f"to fill a {row.target_ms / 1000:.2f}s window holding only "
        f"{row.natural_ms / 1000:.2f}s of speech."
        for row in rows
        if row.unnatural_silence
    ]
