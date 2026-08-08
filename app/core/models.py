"""Data model for subtitle segments and their generated audio.

Nothing here imports Qt or any audio library, so the whole timing model can be
exercised in plain pytest.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from enum import Enum

from app.core.timecode import format_display, format_duration


class SegmentStatus(str, Enum):
    """Lifecycle of a single subtitle's audio."""

    PENDING = "pending"           # never generated
    QUEUED = "queued"             # waiting for a worker
    GENERATING = "generating"     # in flight
    GENERATED = "generated"       # audio exists and matches the current text/settings
    NEEDS_REGEN = "needs_regen"   # text or settings changed since generation
    FAILED = "failed"             # generation raised
    SKIPPED = "skipped"           # user excluded it

    @property
    def label(self) -> str:
        return {
            SegmentStatus.PENDING: "Not generated",
            SegmentStatus.QUEUED: "Queued",
            SegmentStatus.GENERATING: "Generating…",
            SegmentStatus.GENERATED: "Generated",
            SegmentStatus.NEEDS_REGEN: "Needs regeneration",
            SegmentStatus.FAILED: "Failed",
            SegmentStatus.SKIPPED: "Skipped",
        }[self]


class FitPolicy(str, Enum):
    """What to do when generated speech does not match its subtitle window."""

    AUTO = "auto"                 # compress if needed, within the safety limit
    NATURAL = "natural"           # never time-stretch; may overflow (flagged)
    FORCE_FIT = "force_fit"       # compress regardless of how extreme


@dataclass(frozen=True)
class TimingReport:
    """The reconciliation between what the SRT demands and what TTS produced.

    Every field is recorded per §12 so a segment can always be audited.
    """

    target_ms: int          # SRT end - SRT start; authoritative
    generated_ms: int       # raw TTS output length
    final_ms: int           # length actually written to the timeline
    speed_factor: float     # generated_ms / target_ms (>1 means compression used)
    start_sample: int
    end_sample: int
    sample_rate: int
    padded_ms: int = 0      # silence appended after speech
    truncated_ms: int = 0   # should stay 0; non-zero means audio was cut

    @property
    def fits(self) -> bool:
        return self.final_ms <= self.target_ms and self.truncated_ms == 0


@dataclass(frozen=True)
class AudioRef:
    """Where a segment's audio lives on disk.

    The raw TTS render and the effects-processed render are kept separately so
    processing is always non-destructive (§9).
    """

    original_path: str | None = None
    processed_path: str | None = None
    cache_key: str | None = None

    @property
    def playable_path(self) -> str | None:
        return self.processed_path or self.original_path


@dataclass
class Segment:
    """One subtitle: an authoritative time window plus the text to speak in it."""

    start_ms: int
    end_ms: int
    text: str
    uid: str = field(default_factory=lambda: uuid.uuid4().hex)
    status: SegmentStatus = SegmentStatus.PENDING
    source_text: str = ""            # text as originally imported, for revert
    fit_policy: FitPolicy = FitPolicy.AUTO
    voice_override: str | None = None
    timing: TimingReport | None = None
    audio: AudioRef | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if not self.source_text:
            self.source_text = self.text

    @property
    def caption_text(self) -> str:
        """Explicit alias.

        ``Segment.text`` is the *caption* text. Narration text lives on
        :class:`~app.narration.groups.NarrationGroup` and is stored separately,
        so editing a caption never rewrites what was spoken and vice versa.
        """
        return self.text

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms

    @property
    def is_edited(self) -> bool:
        return self.text != self.source_text

    @property
    def needs_generation(self) -> bool:
        return self.status in (
            SegmentStatus.PENDING,
            SegmentStatus.NEEDS_REGEN,
            SegmentStatus.FAILED,
        )

    @property
    def display_start(self) -> str:
        return format_display(self.start_ms)

    @property
    def display_end(self) -> str:
        return format_display(self.end_ms)

    @property
    def display_duration(self) -> str:
        return format_duration(self.duration_ms)

    def with_text(self, text: str) -> Segment:
        """Return a copy with new text, marked for regeneration if it had audio.

        Per §3 this never triggers generation on its own -- it only records that
        the existing render no longer matches the text.
        """
        status = self.status
        if text != self.text and status in (
            SegmentStatus.GENERATED,
            SegmentStatus.GENERATING,
            SegmentStatus.QUEUED,
        ):
            status = SegmentStatus.NEEDS_REGEN
        return replace(self, text=text, status=status)

    def copy(self) -> Segment:
        """Deep-enough copy for undo snapshots (all nested types are frozen)."""
        return replace(self)
