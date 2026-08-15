"""Retiming: move the boundaries to the voice, anchored to the original SRT.

The measured project that motivated this had 3.1s of speech in a 1.7s window
next to windows holding seconds of surplus — no fitting fixes that, only
moving the boundaries. The properties that make it safe to offer:
the total length never changes, captions never reorder or overlap, every
group gets room to speak at natural pace when the video allows it, and a
group keeps its original timing unless something actually forces it to move.
"""

from __future__ import annotations

import pytest

from app.core.document import SubtitleDocument
from app.core.models import Segment
from app.narration.groups import NarrationMode
from app.narration.grouping import GroupingOptions, build_plan
from app.narration.retime import BREATH_MS, plan_retime


def _plan_for(segments):
    # EXACT: one group per caption, so tests control the geometry precisely.
    return build_plan(segments, NarrationMode.EXACT, GroupingOptions())


def _windows(retime):
    return [(s, e) for s, e in retime.group_windows]


# -- the core properties -------------------------------------------------


class TestRetimeProperties:
    def test_a_cramped_group_gets_the_room_it_needs(self):
        """The field case: too many words in a tiny window, surplus next door."""
        segments = [
            Segment(0, 5000, "Plenty of room here."),
            Segment(5000, 6700, "Far too many words for this tiny window."),
            Segment(6700, 12_000, "And plenty of room here too."),
        ]
        speech = [2000, 3100, 2000]
        retime = plan_retime(segments, _plan_for(segments), speech)

        for (start, end), spoken in zip(_windows(retime), speech):
            assert end - start >= spoken + BREATH_MS

    def test_the_total_length_never_changes(self):
        segments = [
            Segment(0, 3000, "One."), Segment(3000, 5000, "Two."),
            Segment(5000, 9000, "Three."),
        ]
        retime = plan_retime(segments, _plan_for(segments), [4000, 3500, 1000])
        assert _windows(retime)[-1][1] <= 9000
        last_caption = retime.caption_times[-1]
        assert last_caption[2] <= 9000

    def test_untroubled_groups_do_not_move(self):
        """Anchoring: only what must move, moves."""
        segments = [
            Segment(0, 4000, "Fits fine."),
            Segment(4000, 8000, "Also fits fine."),
        ]
        retime = plan_retime(segments, _plan_for(segments), [3000, 3000])
        assert not retime.is_worthwhile
        assert _windows(retime) == [(0, 4000), (4000, 8000)]

    def test_drift_recovers_at_the_next_slack_window(self):
        """A shift does not cascade to the end of the project."""
        segments = [
            Segment(0, 2000, "Too many words for two seconds."),
            Segment(2000, 10_000, "A huge window with almost nothing in it."),
            Segment(10_000, 14_000, "Back on the original timing."),
        ]
        retime = plan_retime(segments, _plan_for(segments), [4000, 1000, 3000])
        windows = _windows(retime)
        assert windows[0][1] >= 4000 + BREATH_MS   # the cramped one grew
        assert windows[2][0] == 10_000             # anchored again downstream

    def test_captions_never_overlap_and_never_reorder(self):
        segments = [
            Segment(0, 1000, "a"), Segment(1000, 2200, "b"),
            Segment(2200, 3100, "c"), Segment(3100, 9000, "d"),
        ]
        retime = plan_retime(segments, _plan_for(segments), [2500, 2500, 2500, 800])
        times = retime.caption_times
        previous_end = 0
        for _index, start, end in times:
            assert start >= previous_end
            assert end > start
            previous_end = end

    def test_impossible_speech_shares_one_uniform_squeeze(self):
        """More words than video: everyone slows the same amount, no lurching."""
        segments = [Segment(0, 3000, "a"), Segment(3000, 6000, "b")]
        retime = plan_retime(segments, _plan_for(segments), [5000, 5000])
        assert retime.uniform_squeeze > 1.0
        assert retime.notes
        assert _windows(retime)[-1][1] <= 6000

    def test_interior_captions_scale_with_their_group(self):
        """A merged group's inner boundaries keep their proportions."""
        segments = [
            Segment(0, 2000, "First half of the sentence"),
            Segment(2000, 4000, "and the second half."),
        ]
        plan = build_plan(segments, NarrationMode.NATURAL, GroupingOptions())
        retime = plan_retime(segments, plan, [6000] * len(plan.groups))
        times = dict((i, (s, e)) for i, s, e in retime.caption_times)
        assert times[0][1] == times[1][0], "the shared boundary must stay shared"

    def test_mismatched_measurements_are_refused(self):
        segments = [Segment(0, 3000, "a")]
        retime = plan_retime(segments, _plan_for(segments), [1000, 2000])
        assert not retime.is_worthwhile
        assert retime.notes


# -- applying to the document --------------------------------------------


class TestApplyTimeMap:
    def _document(self):
        document = SubtitleDocument()
        document.load([
            Segment(0, 2000, "One."), Segment(2000, 4000, "Two."),
            Segment(4000, 6000, "Three."),
        ])
        return document

    def test_applied_in_one_undoable_step(self):
        document = self._document()
        changed = document.apply_time_map(
            {0: (0, 1500), 1: (1500, 4500), 2: (4500, 6000)}, "Fit timings"
        )
        assert changed == 3
        assert [s.start_ms for s in document.segments] == [0, 1500, 4500]

        document.undo()
        assert [s.start_ms for s in document.segments] == [0, 2000, 4000]

    def test_an_overlapping_map_is_refused_whole(self):
        from app.core.document import DocumentError

        document = self._document()
        with pytest.raises(DocumentError):
            document.apply_time_map({0: (0, 3000), 1: (2500, 4000)}, "Bad")
        # Nothing was half-applied.
        assert [s.start_ms for s in document.segments] == [0, 2000, 4000]

    def test_a_full_retime_round_trip(self):
        """plan_retime's output must always be applicable."""
        document = self._document()
        segments = document.segments
        retime = plan_retime(
            segments, _plan_for(segments), [3500, 1000, 1500]
        )
        if retime.is_worthwhile:
            document.apply_time_map(retime.as_time_map(), "Fit timings")
        ordered = document.segments
        for earlier, later in zip(ordered, ordered[1:]):
            assert later.start_ms >= earlier.end_ms


# -- pad over stretch ----------------------------------------------------


def test_balanced_no_longer_drawls_at_the_hard_floor():
    """The 0.88x drawl measured on the real project: now quiet instead."""
    from app.audio.timing import fit_options_for, plan_fit

    plan = plan_fit(4500, 9600, fit_options_for("balanced"))
    assert plan.speed_factor >= 0.95
    assert plan.pad_ms > 0


def test_exact_sync_still_stretches_like_before():
    from app.audio.timing import fit_options_for, plan_fit

    plan = plan_fit(4000, 5000, fit_options_for("exact"))
    assert plan.speed_factor == pytest.approx(0.8, abs=0.01)
    assert plan.pad_ms == 0
