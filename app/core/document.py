"""The subtitle document: the editable list of segments and every mutation on it.

All state lives in the undo stack, so ``undo()``/``redo()`` need no special
handling per operation. Mutating methods return the number of segments affected
so the UI can report "3 subtitles merged" without recomputing.

Timestamps are only ever changed by methods the user explicitly invokes (§20,
§43). Nothing here silently reflows the timeline.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Callable, Iterable, Sequence

from app.core.models import Segment, SegmentStatus
from app.core.undo import UndoStack

MIN_SEGMENT_MS = 100  # refuse to create a subtitle shorter than this


class DocumentError(Exception):
    """Raised when a requested edit is not possible."""


class SubtitleDocument:
    """An ordered list of :class:`Segment` with undo/redo and edit operations."""

    def __init__(self, segments: Sequence[Segment] | None = None) -> None:
        initial = [s.copy() for s in (segments or [])]
        self._stack: UndoStack[list[Segment]] = UndoStack(initial)
        self._listeners: list[Callable[[], None]] = []
        self.source_path: Path | None = None
        self.source_format: str = "srt"
        self._saved_revision = 0
        self._revision = 0

    # -- observation -----------------------------------------------------

    def add_listener(self, callback: Callable[[], None]) -> None:
        """Register a callback fired after any change (including undo/redo)."""
        self._listeners.append(callback)

    def _notify(self) -> None:
        self._revision += 1
        for callback in list(self._listeners):
            callback()

    # -- access ----------------------------------------------------------

    @property
    def segments(self) -> list[Segment]:
        """The live segment list. Treat as read-only; edit via the methods below."""
        return self._stack.state

    def __len__(self) -> int:
        return len(self._stack.state)

    def __iter__(self):
        return iter(self._stack.state)

    def at(self, index: int) -> Segment:
        try:
            return self._stack.state[index]
        except IndexError as exc:
            raise DocumentError(f"No subtitle at position {index + 1}") from exc

    def by_uid(self, uid: str) -> Segment | None:
        return next((s for s in self._stack.state if s.uid == uid), None)

    def index_of(self, uid: str) -> int | None:
        return next(
            (i for i, s in enumerate(self._stack.state) if s.uid == uid), None
        )

    @property
    def timeline_end_ms(self) -> int:
        """End of the last subtitle -- the authoritative length of the export."""
        return max((s.end_ms for s in self._stack.state), default=0)

    @property
    def timeline_start_ms(self) -> int:
        return min((s.start_ms for s in self._stack.state), default=0)

    @property
    def total_speech_ms(self) -> int:
        return sum(s.duration_ms for s in self._stack.state)

    @property
    def is_dirty(self) -> bool:
        return self._revision != self._saved_revision

    def mark_saved(self) -> None:
        self._saved_revision = self._revision

    def counts_by_status(self) -> dict[SegmentStatus, int]:
        counts: dict[SegmentStatus, int] = {}
        for segment in self._stack.state:
            counts[segment.status] = counts.get(segment.status, 0) + 1
        return counts

    # -- history ---------------------------------------------------------

    @property
    def can_undo(self) -> bool:
        return self._stack.can_undo

    @property
    def can_redo(self) -> bool:
        return self._stack.can_redo

    @property
    def undo_label(self) -> str:
        return self._stack.undo_label

    @property
    def redo_label(self) -> str:
        return self._stack.redo_label

    def undo(self) -> None:
        self._stack.undo()
        self._notify()

    def redo(self) -> None:
        self._stack.redo()
        self._notify()

    def _commit(self, label: str, segments: list[Segment]) -> None:
        self._stack.push(label, segments)
        self._notify()

    def _snapshot(self) -> list[Segment]:
        return [s.copy() for s in self._stack.state]

    # -- loading ---------------------------------------------------------

    def load(
        self,
        segments: Sequence[Segment],
        path: Path | None = None,
        source_format: str = "srt",
    ) -> None:
        """Replace the whole document and clear history."""
        self._stack.reset([s.copy() for s in segments], "Import")
        self.source_path = path
        self.source_format = source_format
        self._revision = 0
        self._saved_revision = 0
        self._notify()

    # -- text edits ------------------------------------------------------

    def set_text(self, index: int, text: str) -> int:
        """Change a subtitle's text; marks it for regeneration if it had audio."""
        segments = self._snapshot()
        current = segments[index]
        if current.text == text:
            return 0
        segments[index] = current.with_text(text)
        self._commit("Edit text", segments)
        return 1

    def set_times(
        self, index: int, start_ms: int | None = None, end_ms: int | None = None
    ) -> int:
        """Change a subtitle's window. Raises rather than silently clamping."""
        segments = self._snapshot()
        current = segments[index]
        new_start = current.start_ms if start_ms is None else int(start_ms)
        new_end = current.end_ms if end_ms is None else int(end_ms)

        if new_start < 0:
            raise DocumentError("A subtitle cannot start before 00:00:00.000.")
        if new_end - new_start < MIN_SEGMENT_MS:
            raise DocumentError(
                f"A subtitle must be at least {MIN_SEGMENT_MS} ms long. "
                f"That change would make it {new_end - new_start} ms."
            )
        if new_start == current.start_ms and new_end == current.end_ms:
            return 0

        # Retiming invalidates the fit, because the window the audio was
        # compressed into no longer exists.
        status = current.status
        if current.status == SegmentStatus.GENERATED and (
            new_end - new_start != current.duration_ms
        ):
            status = SegmentStatus.NEEDS_REGEN

        segments[index] = replace(
            current, start_ms=new_start, end_ms=new_end, status=status
        )
        self._commit("Adjust timing", segments)
        return 1

    def apply_time_map(
        self, times: dict[int, tuple[int, int]], label: str = "Retime captions"
    ) -> int:
        """Move many subtitle windows in one undoable step.

        Used by retiming, where every boundary moves together — as individual
        ``set_times`` calls the intermediate states would overlap and fail
        validation, and Undo would take dozens of presses to escape.
        """
        segments = self._snapshot()
        changed = 0
        for index, (start_ms, end_ms) in times.items():
            current = segments[index]
            start_ms, end_ms = int(start_ms), int(end_ms)
            if start_ms < 0:
                raise DocumentError("A subtitle cannot start before 00:00:00.000.")
            if end_ms - start_ms < MIN_SEGMENT_MS:
                raise DocumentError(
                    f"Subtitle {index + 1} would be {end_ms - start_ms} ms long; "
                    f"the minimum is {MIN_SEGMENT_MS} ms."
                )
            if (start_ms, end_ms) == (current.start_ms, current.end_ms):
                continue
            segments[index] = replace(
                current, start_ms=start_ms, end_ms=end_ms,
                status=SegmentStatus.NEEDS_REGEN
                if current.status == SegmentStatus.GENERATED
                else current.status,
            )
            changed += 1

        if changed:
            ordered = sorted(segments, key=lambda s: (s.start_ms, s.end_ms))
            for earlier, later in zip(ordered, ordered[1:]):
                if later.start_ms < earlier.end_ms:
                    raise DocumentError(
                        "That retiming would make two subtitles overlap."
                    )
            self._commit(label, segments)
        return changed

    def set_status(self, index: int, status: SegmentStatus) -> int:
        """Update status without creating an undo entry (generation bookkeeping)."""
        segments = self._stack.state
        segments[index] = replace(segments[index], status=status)
        self._notify()
        return 1

    def revert_text(self, indices: Iterable[int]) -> int:
        """Restore the text each subtitle had at import time."""
        segments = self._snapshot()
        changed = 0
        for index in indices:
            current = segments[index]
            if current.text != current.source_text:
                segments[index] = current.with_text(current.source_text)
                changed += 1
        if changed:
            self._commit("Revert text", segments)
        return changed

    def apply_text_map(self, replacements: dict[int, str], label: str) -> int:
        """Apply new text to many rows in one undoable step (used by cleanup)."""
        segments = self._snapshot()
        changed = 0
        for index, text in replacements.items():
            current = segments[index]
            if current.text != text:
                segments[index] = current.with_text(text)
                changed += 1
        if changed:
            self._commit(label, segments)
        return changed

    # -- structural edits ------------------------------------------------

    def split(self, index: int, char_offset: int | None = None) -> int:
        """Split one subtitle into two.

        The time window is divided in proportion to the character split point, so
        the pair still exactly covers the original window -- the timeline length
        never changes.
        """
        segments = self._snapshot()
        current = segments[index]

        text = current.text
        if char_offset is None:
            char_offset = _midpoint_split(text)
        char_offset = max(0, min(len(text), char_offset))

        left_text = text[:char_offset].strip()
        right_text = text[char_offset:].strip()
        if not left_text or not right_text:
            raise DocumentError(
                "Choose a split point that leaves text on both sides."
            )

        duration = current.duration_ms
        if duration < MIN_SEGMENT_MS * 2:
            raise DocumentError(
                "This subtitle is too short to split into two readable parts."
            )

        ratio = len(left_text) / max(1, len(left_text) + len(right_text))
        cut = current.start_ms + int(round(duration * ratio))
        cut = max(current.start_ms + MIN_SEGMENT_MS, min(current.end_ms - MIN_SEGMENT_MS, cut))

        left = Segment(
            start_ms=current.start_ms,
            end_ms=cut,
            text=left_text,
            source_text=left_text,
            fit_policy=current.fit_policy,
            voice_override=current.voice_override,
        )
        right = Segment(
            start_ms=cut,
            end_ms=current.end_ms,
            text=right_text,
            source_text=right_text,
            fit_policy=current.fit_policy,
            voice_override=current.voice_override,
        )
        segments[index : index + 1] = [left, right]
        self._commit("Split subtitle", segments)
        return 2

    def merge(self, indices: Sequence[int]) -> int:
        """Merge consecutive subtitles into one spanning their full window."""
        ordered = sorted(set(indices))
        if len(ordered) < 2:
            raise DocumentError("Select at least two subtitles to merge.")
        if ordered != list(range(ordered[0], ordered[-1] + 1)):
            raise DocumentError("Only consecutive subtitles can be merged.")

        segments = self._snapshot()
        group = segments[ordered[0] : ordered[-1] + 1]
        merged_text = " ".join(s.text.strip() for s in group if s.text.strip())
        merged = Segment(
            start_ms=group[0].start_ms,
            end_ms=group[-1].end_ms,
            text=merged_text,
            source_text=merged_text,
            fit_policy=group[0].fit_policy,
            voice_override=group[0].voice_override,
        )
        segments[ordered[0] : ordered[-1] + 1] = [merged]
        self._commit(f"Merge {len(group)} subtitles", segments)
        return len(group)

    def duplicate(self, index: int) -> int:
        """Insert a copy directly after ``index``.

        The copy takes the second half of the original's window so the two never
        overlap -- an overlapping duplicate would be invalid the instant it existed.
        """
        segments = self._snapshot()
        current = segments[index]
        if current.duration_ms < MIN_SEGMENT_MS * 2:
            raise DocumentError(
                "This subtitle is too short to duplicate without overlapping."
            )
        midpoint = current.start_ms + current.duration_ms // 2
        segments[index] = replace(current, end_ms=midpoint)
        segments.insert(
            index + 1,
            Segment(
                start_ms=midpoint,
                end_ms=current.end_ms,
                text=current.text,
                source_text=current.text,
                fit_policy=current.fit_policy,
                voice_override=current.voice_override,
            ),
        )
        self._commit("Duplicate subtitle", segments)
        return 1

    def delete(self, indices: Iterable[int]) -> int:
        """Remove subtitles. The gap they leave is preserved as silence."""
        targets = sorted(set(indices), reverse=True)
        if not targets:
            return 0
        segments = self._snapshot()
        for index in targets:
            if 0 <= index < len(segments):
                segments.pop(index)
        label = "Delete subtitle" if len(targets) == 1 else f"Delete {len(targets)} subtitles"
        self._commit(label, segments)
        return len(targets)

    def insert_after(self, index: int, text: str = "") -> int:
        """Insert a new subtitle into the gap following ``index``, if one exists."""
        segments = self._snapshot()
        if not segments:
            segments.append(Segment(start_ms=0, end_ms=2000, text=text))
            self._commit("Add subtitle", segments)
            return 1

        current = segments[index]
        following = segments[index + 1] if index + 1 < len(segments) else None
        gap_start = current.end_ms
        gap_end = following.start_ms if following else current.end_ms + 2000
        if gap_end - gap_start < MIN_SEGMENT_MS:
            raise DocumentError(
                "There is no free time after this subtitle. Adjust the timings "
                "first, or split this subtitle instead."
            )
        segments.insert(
            index + 1, Segment(start_ms=gap_start, end_ms=gap_end, text=text)
        )
        self._commit("Add subtitle", segments)
        return 1

    def sort_by_time(self) -> int:
        """Reorder segments by start time. Timestamps themselves are untouched."""
        segments = self._snapshot()
        ordered = sorted(segments, key=lambda s: (s.start_ms, s.end_ms))
        if [s.uid for s in ordered] == [s.uid for s in segments]:
            return 0
        self._commit("Sort by start time", ordered)
        return len(ordered)


def _midpoint_split(text: str) -> int:
    """Find a natural split point near the middle of ``text``.

    Prefers a sentence boundary, then a word boundary, then the exact middle.
    """
    if not text:
        return 0
    middle = len(text) // 2

    best_sentence = -1
    for marker in (". ", "? ", "! ", "; "):
        position = 0
        while True:
            found = text.find(marker, position)
            if found == -1:
                break
            candidate = found + len(marker)
            if best_sentence == -1 or abs(candidate - middle) < abs(best_sentence - middle):
                best_sentence = candidate
            position = found + 1
    if best_sentence != -1 and 0 < best_sentence < len(text):
        return best_sentence

    left = text.rfind(" ", 0, middle)
    right = text.find(" ", middle)
    candidates = [c for c in (left, right) if c > 0]
    if not candidates:
        return middle
    return min(candidates, key=lambda c: abs(c - middle)) + 1
