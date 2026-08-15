"""Move the caption timings to fit the voice, instead of the voice to fit them.

AI-written timings guess how long words take, and guess wrong in both
directions at once: on one measured project a group had 3.1 seconds of speech
in a 1.7-second window while its neighbours sat on seconds of surplus. No
amount of clever fitting fixes that — bending the voice is what produced the
drawl-then-gabble complaint in the first place. When the user has said the
timings may flex, the honest fix is to move the boundaries.

The algorithm is anchored: every group keeps its original start unless it, or
one before it, genuinely needs more room — so most captions do not move at
all, the ones that do move by tenths of a second, and the total length never
changes. On the project above it moved six of ten group starts by nothing and
the worst by 1.7 seconds, while giving every group enough room to speak at
natural pace.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Sequence

from app.core.models import Segment
from app.narration.groups import GroupWindow, NarrationPlan

logger = logging.getLogger(__name__)

#: Room to breathe after each group's words, so speech never ends exactly on
#: the boundary of the next.
BREATH_MS = 300

#: A caption shorter than this cannot be read; interior boundaries keep it.
MIN_CAPTION_MS = 400


@dataclass(frozen=True)
class RetimePlan:
    """New caption windows, and the honest summary of what moved."""

    #: (segment index, new start, new end) for every caption, in order.
    caption_times: tuple[tuple[int, int, int], ...] = ()
    #: The new group windows, index-aligned with the narration plan.
    group_windows: tuple[tuple[int, int], ...] = ()
    max_shift_ms: int = 0
    moved_groups: int = 0
    #: > 1.0 when the words genuinely do not fit the video and every group
    #: shares the same gentle squeeze — consistent, rather than lurching.
    uniform_squeeze: float = 1.0
    notes: list[str] = field(default_factory=list)

    @property
    def is_worthwhile(self) -> bool:
        return self.moved_groups > 0

    def as_time_map(self) -> dict[int, tuple[int, int]]:
        return {index: (start, end) for index, start, end in self.caption_times}


def plan_retime(
    segments: Sequence[Segment],
    narration: NarrationPlan,
    speech_ms: Sequence[int],
    breath_ms: int = BREATH_MS,
) -> RetimePlan:
    """Fit the windows to the measured speech, anchored to the original times.

    ``speech_ms`` is the natural spoken length of each group, index-aligned
    with ``narration.groups`` — exactly what a generation pass just measured.
    """
    groups = narration.groups
    if not groups or len(speech_ms) != len(groups):
        return RetimePlan(notes=["Nothing to retime."])

    window = GroupWindow(list(segments))
    timeline_ms = max(segment.end_ms for segment in segments)
    count = len(groups)

    originals = [(window.start_ms(g), window.end_ms(g)) for g in groups]
    desired = [max(1, speech) + breath_ms for speech in speech_ms]

    # If the words genuinely cannot fit the video, share one gentle squeeze
    # across every group rather than letting it land on whoever comes last.
    available = timeline_ms - originals[0][0]
    squeeze = 1.0
    if sum(desired) > available:
        squeeze = sum(desired) / available
        desired = [int(d / squeeze) for d in desired]

    # How much room the rest of the plan still needs, from each group onward.
    suffix = [0] * (count + 1)
    for i in range(count - 1, -1, -1):
        suffix[i] = suffix[i + 1] + desired[i]

    new_windows: list[tuple[int, int]] = []
    cursor = originals[0][0]
    for i, (orig_start, orig_end) in enumerate(originals):
        latest_start = timeline_ms - suffix[i]
        # Anchor: sit on the original start whenever the previous group has
        # left room for it; drift only as far as the words force.
        start = min(max(cursor, min(orig_start, latest_start)), latest_start)
        if orig_start >= cursor:
            start = min(orig_start, latest_start)

        earliest_end = start + desired[i]
        latest_end = timeline_ms - suffix[i + 1]
        # Keep the original end when it already gives enough room — that is
        # what keeps a half-empty window padded instead of shuffled.
        end = min(max(orig_end, earliest_end), latest_end)
        new_windows.append((start, end))
        cursor = end

    # Spread each group's captions across its new window in the same
    # proportions they occupied in the old one.
    caption_times: list[tuple[int, int, int]] = []
    moved = 0
    max_shift = 0
    by_uid = {segment.uid: j for j, segment in enumerate(segments)}
    for i, group in enumerate(groups):
        old_start, old_end = originals[i]
        new_start, new_end = new_windows[i]
        shift = abs(new_start - old_start)
        max_shift = max(max_shift, shift, abs(new_end - old_end))
        if (new_start, new_end) != (old_start, old_end):
            moved += 1

        old_span = max(1, old_end - old_start)
        new_span = new_end - new_start
        for uid in group.segment_uids:
            seg_index = by_uid[uid]
            segment = segments[seg_index]
            fraction_start = (segment.start_ms - old_start) / old_span
            fraction_end = (segment.end_ms - old_start) / old_span
            start = new_start + round(fraction_start * new_span)
            end = new_start + round(fraction_end * new_span)
            caption_times.append((seg_index, start, end))

    caption_times = _enforce_minimums(caption_times, timeline_ms)

    notes: list[str] = []
    if squeeze > 1.0:
        notes.append(
            f"The words take {squeeze:.2f}× longer than the video allows, so "
            "every caption carries the same slight speed-up."
        )
    return RetimePlan(
        caption_times=tuple(caption_times),
        group_windows=tuple(new_windows),
        max_shift_ms=max_shift,
        moved_groups=moved,
        uniform_squeeze=squeeze,
        notes=notes,
    )


def _enforce_minimums(
    times: list[tuple[int, int, int]], timeline_ms: int
) -> list[tuple[int, int, int]]:
    """Keep every caption readable and the sequence strictly ordered."""
    fixed: list[tuple[int, int, int]] = []
    previous_end = 0
    for position, (index, start, end) in enumerate(times):
        start = max(start, previous_end)
        end = max(end, start + MIN_CAPTION_MS)
        remaining = len(times) - position - 1
        latest_end = timeline_ms - remaining * MIN_CAPTION_MS
        end = min(end, max(latest_end, start + MIN_CAPTION_MS))
        fixed.append((index, start, end))
        previous_end = end
    return fixed
