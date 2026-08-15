"""Fitting generated speech into its window on the master clock.

Pure arithmetic, no audio libraries, so every timing rule is unit-testable.

The SRT window is authoritative. A group's audio is placed at the group's start
sample and must end at or before the group's end sample. It is never positioned
relative to the previous group's audio -- that is what makes cumulative drift
impossible.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from app.narration.groups import SpeedSafety, classify_speed

# ffmpeg's atempo accepts 0.5 .. 2.0 per instance; anything outside is chained.
ATEMPO_MIN = 0.5
ATEMPO_MAX = 2.0

#: Comfortable slow-down limit: speech stretched this far still sounds natural.
MIN_STRETCH_FACTOR = 0.90
#: Absolute slow-down limit. Between this and MIN_STRETCH_FACTOR the speech is
#: still stretched rather than padded, because a second of dead air inside a
#: group is far more noticeable than speech running slightly slow.
HARD_MIN_STRETCH_FACTOR = 0.75
#: Trailing silence above this inside a group is reported as a defect, not
#: accepted as normal synchronisation.
MAX_NATURAL_TRAILING_MS = 500
#: Differences this small are not worth resampling for.
NEGLIGIBLE_MS = 15


class FitAction(str, Enum):
    NONE = "none"              # already the right length
    COMPRESS = "compress"      # speech was too long; speed it up
    STRETCH = "stretch"        # speech was too short; slow it down to fill
    PAD = "pad"                # append silence at the end of the group
    STRETCH_AND_PAD = "stretch_and_pad"


class FillPolicy(str, Enum):
    """What to do when a group's speech is shorter than its window."""

    #: Slow the speech slightly to fill the window, then pad whatever is left.
    #: Removes the small pause that trailing silence creates at a group join.
    STRETCH_THEN_PAD = "stretch_then_pad"
    #: Leave the speech at its natural rate and append silence -- the original
    #: proof-of-concept's behaviour.
    PAD_ONLY = "pad_only"


@dataclass(frozen=True)
class FitOptions:
    fill_policy: FillPolicy = FillPolicy.STRETCH_THEN_PAD
    min_stretch_factor: float = MIN_STRETCH_FACTOR
    hard_min_stretch_factor: float = HARD_MIN_STRETCH_FACTOR
    #: Trailing silence beyond this inside a group is flagged as unnatural.
    max_trailing_silence_ms: int = MAX_NATURAL_TRAILING_MS
    #: Refuse to compress past this without explicit confirmation.
    max_compress_factor: float = 1.30
    allow_extreme_compression: bool = False
    #: How much of the silent gap AFTER a group's window its speech may borrow
    #: before being sped up. The gap belongs to nobody — the SRT put silence
    #: there — and letting a long sentence finish in it at natural pace sounds
    #: far better than rushing the sentence to the caption boundary. 0 keeps
    #: the boundary exact; the captions themselves are never moved either way.
    gap_borrow_fraction: float = 0.0


#: Ready-made pacing choices, from strict sync to natural delivery.
#:
#: "balanced" is the default: barely-perceptible speed changes, and long
#: sentences may finish in the silence after their caption. "exact" is the old
#: behaviour — fill every window to the millisecond, stretching down to 0.75×
#: and squeezing to 1.30×, which is where "sometimes it drawls, sometimes it
#: gabbles" came from when the timings were an AI's guesses. "natural" barely
#: bends the voice at all and pads or borrows instead.
PACING_PRESETS: dict[str, FitOptions] = {
    "exact": FitOptions(),
    "balanced": FitOptions(
        min_stretch_factor=0.95,
        hard_min_stretch_factor=0.88,
        gap_borrow_fraction=0.65,
    ),
    "natural": FitOptions(
        min_stretch_factor=0.98,
        hard_min_stretch_factor=0.97,
        gap_borrow_fraction=0.90,
        max_trailing_silence_ms=1500,
    ),
}


def fit_options_for(pacing: str) -> FitOptions:
    """The FitOptions for a pacing name; unknown names get the default."""
    return PACING_PRESETS.get(pacing, PACING_PRESETS["balanced"])


@dataclass(frozen=True)
class FitPlan:
    """How one group's audio will be made to fit its window."""

    target_ms: int
    generated_ms: int
    speed_factor: float      # >1 speeds up, <1 slows down
    fitted_ms: int           # length after the speed change
    pad_ms: int              # silence appended at the END of the group only
    action: FitAction
    safety: SpeedSafety
    needs_confirmation: bool
    message: str = ""
    #: True when the trailing silence is large enough to be heard as a hole in
    #: the narration rather than as natural phrasing.
    unnatural_silence: bool = False
    #: How far the speech runs past its window into the silent gap after it —
    #: sanctioned borrowing, never a collision with the next group.
    spill_ms: int = 0

    @property
    def final_ms(self) -> int:
        return self.fitted_ms + self.pad_ms

    @property
    def silence_inserted_ms(self) -> int:
        """Silence added to fill the window -- not silence the SRT asked for.

        Real gaps between subtitles live *between* group windows and are part of
        the zero-filled timeline. Anything counted here was invented to pad.
        """
        return self.pad_ms

    @property
    def fits(self) -> bool:
        return self.final_ms <= self.target_ms + self.spill_ms


def plan_fit(
    generated_ms: int,
    target_ms: int,
    options: FitOptions | None = None,
    available_ms: int | None = None,
) -> FitPlan:
    """Decide how to reconcile generated speech with its SRT window.

    ``available_ms`` is the room from this group's start to the next group's —
    the window plus the silent gap after it. Compression may borrow from that
    gap, per ``options.gap_borrow_fraction``; nothing else uses it.
    """
    options = options or FitOptions()

    if target_ms <= 0:
        return FitPlan(
            target_ms=max(0, target_ms),
            generated_ms=generated_ms,
            speed_factor=1.0,
            fitted_ms=0,
            pad_ms=0,
            action=FitAction.NONE,
            safety=SpeedSafety.SAFE,
            needs_confirmation=False,
            message="This group has no time on the timeline.",
        )
    if generated_ms <= 0:
        return FitPlan(
            target_ms=target_ms,
            generated_ms=0,
            speed_factor=1.0,
            fitted_ms=0,
            pad_ms=target_ms,
            action=FitAction.PAD,
            safety=SpeedSafety.SAFE,
            needs_confirmation=False,
            message="No audio was produced; the window will be silent.",
        )

    difference = generated_ms - target_ms

    if abs(difference) <= NEGLIGIBLE_MS:
        return FitPlan(
            target_ms=target_ms,
            generated_ms=generated_ms,
            speed_factor=1.0,
            fitted_ms=min(generated_ms, target_ms),
            pad_ms=max(0, target_ms - generated_ms),
            action=FitAction.NONE,
            safety=SpeedSafety.SAFE,
            needs_confirmation=False,
        )

    if difference > 0:
        return _plan_compression(generated_ms, target_ms, options, available_ms)
    return _plan_fill(generated_ms, target_ms, options)


def _plan_compression(
    generated_ms: int,
    target_ms: int,
    options: FitOptions,
    available_ms: int | None,
) -> FitPlan:
    """Speech is longer than the window: borrow the gap, then speed up.

    Never truncate. The gap after the window is used first because slightly
    late is barely noticeable and slightly fast is very noticeable.
    """
    allowed_ms = target_ms
    if (
        available_ms is not None
        and available_ms > target_ms
        and options.gap_borrow_fraction > 0
    ):
        gap = available_ms - target_ms
        allowed_ms = target_ms + int(gap * options.gap_borrow_fraction)

    effective_ms = min(generated_ms, allowed_ms)
    spill_ms = effective_ms - target_ms
    factor = generated_ms / effective_ms
    safety = classify_speed(factor)
    needs_confirmation = (
        factor > options.max_compress_factor and not options.allow_extreme_compression
    )

    borrowed = f" (after borrowing {spill_ms / 1000:.1f}s of the following gap)" if spill_ms else ""
    message = ""
    if safety is SpeedSafety.WARNING:
        message = f"Speech runs {factor:.2f}× long{borrowed}; a slight speed-up is applied."
    elif safety is SpeedSafety.STRONG_WARNING:
        message = (
            f"Speech runs {factor:.2f}× long{borrowed}. It will be noticeably fast — "
            "consider shortening the text or lengthening the window."
        )
    elif safety is SpeedSafety.NEEDS_CONFIRMATION:
        message = (
            f"Speech needs {factor:.2f}× compression to fit{borrowed}. That is too "
            "fast to apply automatically."
        )

    return FitPlan(
        target_ms=target_ms,
        generated_ms=generated_ms,
        speed_factor=factor,
        fitted_ms=effective_ms,
        pad_ms=0,
        action=FitAction.NONE if abs(factor - 1.0) < 1e-9 else FitAction.COMPRESS,
        safety=safety,
        needs_confirmation=needs_confirmation,
        message=message,
        spill_ms=spill_ms,
    )


def _plan_fill(generated_ms: int, target_ms: int, options: FitOptions) -> FitPlan:
    """Speech is shorter than the window: stretch a little, then pad the rest."""
    if options.fill_policy is FillPolicy.PAD_ONLY:
        return FitPlan(
            target_ms=target_ms,
            generated_ms=generated_ms,
            speed_factor=1.0,
            fitted_ms=generated_ms,
            pad_ms=target_ms - generated_ms,
            action=FitAction.PAD,
            safety=SpeedSafety.SAFE,
            needs_confirmation=False,
        )

    exact_factor = generated_ms / target_ms  # < 1

    # Stretching to fill is preferred over padding at every step. A group's
    # window is a speech window, not a slot to pour silence into: dead air in
    # the middle of a sentence is far more damaging than speech running slow.
    if exact_factor >= options.min_stretch_factor:
        factor = exact_factor              # comfortable: fill it exactly
    elif exact_factor >= options.hard_min_stretch_factor:
        factor = exact_factor              # slow, but still better than a hole
    else:
        factor = options.hard_min_stretch_factor  # floor it, then pad the rest

    fitted_ms = min(target_ms, int(round(generated_ms / factor)))
    pad_ms = max(0, target_ms - fitted_ms)

    if pad_ms <= NEGLIGIBLE_MS:
        action = FitAction.STRETCH
        fitted_ms, pad_ms = target_ms, 0
    elif abs(factor - 1.0) < 1e-9:
        action = FitAction.PAD
    else:
        action = FitAction.STRETCH_AND_PAD

    unnatural = pad_ms > options.max_trailing_silence_ms
    message = ""
    if unnatural:
        message = (
            f"{pad_ms / 1000:.1f}s of silence had to be inserted after this group: "
            f"the narration is only {generated_ms / 1000:.1f}s but its window is "
            f"{target_ms / 1000:.1f}s, which is too short a gap to close by "
            "slowing down. Merge this group with the next one, or check the "
            "caption timings."
        )

    return FitPlan(
        target_ms=target_ms,
        generated_ms=generated_ms,
        speed_factor=factor,
        fitted_ms=fitted_ms,
        pad_ms=pad_ms,
        action=action,
        safety=SpeedSafety.SAFE,  # slowing down is never a quality risk
        needs_confirmation=False,
        message=message,
        unnatural_silence=unnatural,
    )


def atempo_stages(factor: float) -> list[float]:
    """Break ``factor`` into atempo stages each within ffmpeg's legal range.

    ``atempo`` only accepts 0.5–2.0 per instance, so 4× becomes ``2.0, 2.0``.
    Preserved from the proof-of-concept, which this behaviour must not regress.

    The rule lives here rather than beside either caller, because the filter is
    now driven two ways — in-process through PyAV's filter graph, and through
    the command line as a fallback — and they must chain identically.
    """
    if factor <= 0:
        raise ValueError(f"Speed factor must be positive, got {factor}")

    stages: list[float] = []
    remaining = float(factor)
    while remaining > ATEMPO_MAX:
        stages.append(ATEMPO_MAX)
        remaining /= ATEMPO_MAX
    while remaining < ATEMPO_MIN:
        stages.append(ATEMPO_MIN)
        remaining /= ATEMPO_MIN
    stages.append(remaining)
    return stages


def atempo_chain(factor: float) -> list[str]:
    """The atempo stages as ffmpeg filter arguments."""
    return [f"atempo={stage:.8f}" for stage in atempo_stages(factor)]


def atempo_filter(factor: float) -> str:
    """The full ffmpeg ``-filter:a`` argument for a speed change."""
    return ",".join(atempo_chain(factor))
