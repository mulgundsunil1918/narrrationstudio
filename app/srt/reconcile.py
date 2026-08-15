"""Bring an AI-edited script back in without letting it touch the clock.

An AI asked to polish a subtitle file will usually return the timestamps
untouched, and will occasionally not: it renumbers, merges two captions into
one, rounds a millisecond, or helpfully "fixes" a timing to suit the new
wording. Any of those, applied blindly, would slide the narration out of step
with the video — which is the one thing this app exists to prevent.

So a returned file is never loaded as a document. It is matched against the
captions already open, and only the *text* of matched captions is taken. What
did not line up is reported rather than guessed at, because a caption silently
dropped is worse than a caption the user is told about.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from app.core.models import Segment
from app.core.timecode import format_display

#: Two captions are the same caption if their starts are within this. Covers
#: rounding, not editing.
MATCH_TOLERANCE_MS = 60


@dataclass
class Change:
    index: int          # position in the open document
    before: str
    after: str


@dataclass
class Reconciliation:
    """What can safely be taken from a returned file, and what cannot."""

    changes: list[Change] = field(default_factory=list)
    matched: int = 0
    #: Captions in the open script that the returned file had nothing for.
    missing: list[int] = field(default_factory=list)
    #: Entries in the returned file that matched no open caption.
    extra: int = 0
    #: Matched captions whose timestamps came back different. Reported, never applied.
    retimed: list[int] = field(default_factory=list)
    strategy: str = ""
    problems: list[str] = field(default_factory=list)

    @property
    def changed(self) -> int:
        return len(self.changes)

    @property
    def is_usable(self) -> bool:
        return self.matched > 0

    @property
    def is_clean(self) -> bool:
        """Whether the file came back exactly as it was asked to."""
        return not self.missing and not self.extra and not self.retimed

    def as_text_map(self) -> dict[int, str]:
        return {change.index: change.after for change in self.changes}


def reconcile(current: Sequence[Segment], returned: Sequence[Segment]) -> Reconciliation:
    """Work out which returned text belongs to which open caption.

    Two strategies, in order of trustworthiness. Timestamps are the real
    identity of a caption, so they are tried first; position is the fallback for
    a file whose timings were rewritten wholesale, where the order is all that
    is left to go on.
    """
    if not current:
        return Reconciliation(
            problems=["There is no script open to bring these words back into."]
        )
    if not returned:
        return Reconciliation(
            problems=["That file has no subtitles in it."]
        )

    by_time = _match_by_time(current, returned)
    # A file that lines up on time for nearly everything is the good case. If it
    # does not, its timings were rewritten and position is the better guide.
    if by_time.matched >= len(current) * 0.9:
        result = by_time
    elif len(returned) == len(current):
        result = _match_by_position(current, returned)
    else:
        result = by_time

    _describe(result, current, returned)
    return result


def _match_by_time(
    current: Sequence[Segment], returned: Sequence[Segment]
) -> Reconciliation:
    result = Reconciliation(strategy="time")
    remaining = sorted(range(len(returned)), key=lambda i: returned[i].start_ms)
    used: set[int] = set()

    for index, segment in enumerate(current):
        best = None
        best_distance = MATCH_TOLERANCE_MS + 1
        for position in remaining:
            if position in used:
                continue
            distance = abs(returned[position].start_ms - segment.start_ms)
            if distance < best_distance:
                best, best_distance = position, distance
        if best is None:
            result.missing.append(index)
            continue
        used.add(best)
        result.matched += 1
        _take_text(result, index, segment, returned[best])
        if returned[best].end_ms != segment.end_ms:
            result.retimed.append(index)

    result.extra = len(returned) - len(used)
    return result


def _match_by_position(
    current: Sequence[Segment], returned: Sequence[Segment]
) -> Reconciliation:
    result = Reconciliation(strategy="position")
    for index, (segment, replacement) in enumerate(zip(current, returned)):
        result.matched += 1
        _take_text(result, index, segment, replacement)
        if (replacement.start_ms, replacement.end_ms) != (segment.start_ms, segment.end_ms):
            result.retimed.append(index)
    result.missing = list(range(len(returned), len(current)))
    result.extra = max(0, len(returned) - len(current))
    return result


def _take_text(
    result: Reconciliation, index: int, segment: Segment, replacement: Segment
) -> None:
    text = replacement.text.strip()
    if text and text != segment.text:
        result.changes.append(Change(index=index, before=segment.text, after=text))


def _describe(
    result: Reconciliation, current: Sequence[Segment], returned: Sequence[Segment]
) -> None:
    """Turn the counts into sentences, so nothing has to be inferred from numbers."""
    if not result.matched:
        result.problems.append(
            "None of the subtitles in that file line up with the script you have "
            "open. It may be from a different video."
        )
        return

    if result.missing:
        first = result.missing[0]
        where = format_display(current[first].start_ms)[:-4]
        result.problems.append(
            f"{len(result.missing)} subtitle(s) were not in the returned file, "
            f"starting at {where}. Those keep their current wording."
        )
    if result.extra:
        result.problems.append(
            f"The returned file has {result.extra} subtitle(s) that do not match "
            "anything in your script. They were ignored — nothing was added."
        )
    if result.retimed:
        result.problems.append(
            f"{len(result.retimed)} subtitle(s) came back with different timings. "
            "Your original timings were kept, so the narration still lines up."
        )


def to_plain_text(segments: Sequence[Segment]) -> str:
    """The script as readable prose, one caption per line, no timings.

    For someone who only wants to read or reword it. Coming back the other way
    depends on the line count being preserved, which is stated on screen.
    """
    return "\n".join(segment.text.strip() for segment in segments) + "\n"


def from_plain_text(current: Sequence[Segment], content: str) -> Reconciliation:
    """Take reworded lines back, matched to captions by position.

    Plain text carries no timing, so line order is the only thing tying a line
    to a caption. That works exactly as long as the line count is unchanged, and
    fails loudly when it is not rather than quietly shifting the whole script by
    one.
    """
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    if not lines:
        return Reconciliation(problems=["That file has no text in it."])

    if len(lines) != len(current):
        return Reconciliation(
            strategy="lines",
            problems=[
                f"That file has {len(lines)} lines but your script has "
                f"{len(current)} subtitles. Without matching lines there is no "
                "way to tell which words belong to which moment in the video. "
                "Ask for the .srt version instead — it carries the timings."
            ],
        )

    result = Reconciliation(strategy="lines", matched=len(lines))
    for index, (segment, text) in enumerate(zip(current, lines)):
        if text != segment.text:
            result.changes.append(Change(index=index, before=segment.text, after=text))
    return result
