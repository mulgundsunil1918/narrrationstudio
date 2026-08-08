"""Narration groups: the bridge between the caption timeline and the audio.

The two timelines are deliberately separate:

* The **caption timeline** is the SRT. It is the master clock and never moves.
* The **narration timeline** is a list of :class:`NarrationGroup`, each covering
  one or more consecutive captions and spoken as one continuous utterance.

A group's window is always read from the captions it covers::

    group_start = first caption's start
    group_end   = last caption's end

It is never computed from the length of previously generated audio. That is the
whole anti-drift guarantee: every group is positioned absolutely against the
SRT, so an error in one group cannot push any other group off its mark.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Iterable, Sequence

from app.core.models import AudioRef, Segment, SegmentStatus


class NarrationMode(str, Enum):
    """How captions are turned into narration groups (§ Narration modes)."""

    EXACT = "exact"      # one group per caption -- reproduces the original PoC
    NATURAL = "natural"  # grammar-aware grouping (default)
    MANUAL = "manual"    # groups the user set by hand

    @property
    def label(self) -> str:
        return {
            NarrationMode.EXACT: "Exact Subtitle Timing",
            NarrationMode.NATURAL: "Natural Continuous Narration",
            NarrationMode.MANUAL: "Manual Narration Groups",
        }[self]

    @property
    def description(self) -> str:
        return {
            NarrationMode.EXACT: (
                "Every subtitle is spoken separately. Perfectly predictable, but "
                "you will hear a small pause at each subtitle boundary."
            ),
            NarrationMode.NATURAL: (
                "Subtitles that continue the same sentence are spoken together, "
                "so the narration flows. Captions still change on their own "
                "timestamps."
            ),
            NarrationMode.MANUAL: (
                "You decide which subtitles belong together. Auto-grouping is "
                "left alone."
            ),
        }[self]


class SpeedSafety(str, Enum):
    """How aggressive a time-compression ratio is (§ TTS generation)."""

    SAFE = "safe"                  # <= 1.08x
    WARNING = "warning"            # 1.08 - 1.15x
    STRONG_WARNING = "strong"      # 1.15 - 1.30x
    NEEDS_CONFIRMATION = "confirm" # > 1.30x

    @property
    def label(self) -> str:
        return {
            SpeedSafety.SAFE: "Safe",
            SpeedSafety.WARNING: "Slightly fast",
            SpeedSafety.STRONG_WARNING: "Noticeably fast",
            SpeedSafety.NEEDS_CONFIRMATION: "Too fast — needs confirmation",
        }[self]


SPEED_SAFE_MAX = 1.08
SPEED_WARNING_MAX = 1.15
SPEED_STRONG_MAX = 1.30


def classify_speed(factor: float) -> SpeedSafety:
    """Bucket a required speed-up factor. Slowing down is always safe."""
    if factor <= SPEED_SAFE_MAX:
        return SpeedSafety.SAFE
    if factor <= SPEED_WARNING_MAX:
        return SpeedSafety.WARNING
    if factor <= SPEED_STRONG_MAX:
        return SpeedSafety.STRONG_WARNING
    return SpeedSafety.NEEDS_CONFIRMATION


@dataclass(frozen=True)
class GroupTiming:
    """Reconciliation record for one generated group."""

    target_ms: int        # from the SRT; authoritative
    generated_ms: int     # raw TTS length
    final_ms: int         # length actually placed on the timeline
    speed_factor: float
    start_sample: int
    end_sample: int
    sample_rate: int
    padded_ms: int = 0    # silence appended at the END of the group only
    truncated_ms: int = 0

    @property
    def safety(self) -> SpeedSafety:
        return classify_speed(self.speed_factor)

    @property
    def fits(self) -> bool:
        return self.final_ms <= self.target_ms and self.truncated_ms == 0


@dataclass
class NarrationGroup:
    """One continuous utterance covering one or more consecutive captions."""

    segment_uids: list[str]
    uid: str = field(default_factory=lambda: uuid.uuid4().hex)
    narration_text: str = ""
    #: True once the user has hand-edited the narration text, after which it is
    #: never silently rebuilt from the captions.
    text_is_custom: bool = False
    status: SegmentStatus = SegmentStatus.PENDING
    timing: GroupTiming | None = None
    audio: AudioRef | None = None
    error: str | None = None
    #: Why the auto-grouper joined these captions, for display.
    reasons: tuple[str, ...] = ()
    #: True when this group begins mid-sentence because the length cap forced a
    #: cut, not because the speaker paused. The narration will have a small
    #: unnatural break here -- the UI surfaces it so the user can raise the cap
    #: or merge the groups by hand.
    forced_cut: bool = False

    @property
    def size(self) -> int:
        return len(self.segment_uids)

    @property
    def is_single(self) -> bool:
        return len(self.segment_uids) == 1

    @property
    def needs_generation(self) -> bool:
        return self.status in (
            SegmentStatus.PENDING,
            SegmentStatus.NEEDS_REGEN,
            SegmentStatus.FAILED,
        )

    def copy(self) -> "NarrationGroup":
        return replace(self, segment_uids=list(self.segment_uids))


def build_narration_text(segments: Sequence[Segment]) -> str:
    """Join caption texts into one utterance.

    Whitespace is collapsed and a space is inserted at each caption boundary so
    the boundary itself leaves no trace in the spoken text -- that boundary is a
    display artefact of the SRT, not a pause the narrator took.
    """
    joined = " ".join(segment.text.strip() for segment in segments if segment.text.strip())
    return re.sub(r"\s+", " ", joined).strip()


@dataclass
class NarrationPlan:
    """The full set of groups covering a document, plus the mode that made it."""

    groups: list[NarrationGroup] = field(default_factory=list)
    mode: NarrationMode = NarrationMode.NATURAL

    def __len__(self) -> int:
        return len(self.groups)

    def __iter__(self):
        return iter(self.groups)

    def group_for_segment(self, uid: str) -> NarrationGroup | None:
        return next((g for g in self.groups if uid in g.segment_uids), None)

    def group_index_for_segment(self, uid: str) -> int | None:
        return next(
            (i for i, g in enumerate(self.groups) if uid in g.segment_uids), None
        )

    def by_uid(self, uid: str) -> NarrationGroup | None:
        return next((g for g in self.groups if g.uid == uid), None)

    def index_of(self, uid: str) -> int | None:
        return next((i for i, g in enumerate(self.groups) if g.uid == uid), None)

    def copy(self) -> "NarrationPlan":
        return NarrationPlan(groups=[g.copy() for g in self.groups], mode=self.mode)


class GroupWindow:
    """Resolves a group's absolute window against the caption timeline.

    Kept as a helper rather than fields on the group so the window can never go
    stale: it is recomputed from the captions every time it is asked for.
    """

    def __init__(self, segments: Sequence[Segment]) -> None:
        self._by_uid = {segment.uid: segment for segment in segments}
        self._order = {segment.uid: i for i, segment in enumerate(segments)}

    def segments_of(self, group: NarrationGroup) -> list[Segment]:
        found = [self._by_uid[uid] for uid in group.segment_uids if uid in self._by_uid]
        return sorted(found, key=lambda s: self._order[s.uid])

    def indices_of(self, group: NarrationGroup) -> list[int]:
        return sorted(
            self._order[uid] for uid in group.segment_uids if uid in self._order
        )

    def start_ms(self, group: NarrationGroup) -> int:
        members = self.segments_of(group)
        return members[0].start_ms if members else 0

    def end_ms(self, group: NarrationGroup) -> int:
        members = self.segments_of(group)
        return members[-1].end_ms if members else 0

    def target_ms(self, group: NarrationGroup) -> int:
        """The window the group's audio must fit inside. Always from the SRT."""
        return max(0, self.end_ms(group) - self.start_ms(group))

    def narration_text(self, group: NarrationGroup) -> str:
        """The text to speak: the user's override, or the joined captions."""
        if group.text_is_custom and group.narration_text:
            return group.narration_text
        return build_narration_text(self.segments_of(group))

    def is_stale(self, group: NarrationGroup) -> bool:
        """True when the captions no longer match the text that was generated."""
        if group.status is not SegmentStatus.GENERATED:
            return False
        return self.narration_text(group) != group.narration_text


def resolve_uids(segments: Iterable[Segment]) -> list[str]:
    return [segment.uid for segment in segments]
