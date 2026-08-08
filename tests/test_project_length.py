"""The group cap must never become a project-length limit.

MAX_NARRATION_GROUP_DURATION bounds one TTS generation, nothing else. These
tests pin that down at sizes far past any cap so a future change cannot quietly
turn the cap into a ceiling on the video.
"""

import pytest

from app.core.models import Segment
from app.narration.groups import GroupWindow, NarrationMode
from app.narration.grouping import (
    DEFAULT_MAX_GROUP_MS,
    GroupingOptions,
    build_plan,
)


def timeline(minutes: float, caption_ms: int = 5000) -> list[Segment]:
    """A contiguous transcript-style SRT: every caption continues the last."""
    count = int(minutes * 60 * 1000 / caption_ms)
    return [
        Segment(
            i * caption_ms,
            (i + 1) * caption_ms,
            f"section {i} continues onward with the",
        )
        for i in range(count)
    ]


class TestNoProjectLengthLimit:
    @pytest.mark.parametrize("minutes", [1, 5, 20, 60])
    def test_full_timeline_is_covered(self, minutes):
        segments = timeline(minutes)
        plan = build_plan(segments)
        window = GroupWindow(segments)
        assert window.start_ms(plan.groups[0]) == segments[0].start_ms
        assert window.end_ms(plan.groups[-1]) == segments[-1].end_ms

    @pytest.mark.parametrize("minutes", [1, 5, 20, 60])
    def test_every_caption_is_in_exactly_one_group(self, minutes):
        segments = timeline(minutes)
        plan = build_plan(segments)
        uids = [uid for group in plan for uid in group.segment_uids]
        assert uids == [segment.uid for segment in segments]

    @pytest.mark.parametrize("minutes", [1, 5, 20, 60])
    def test_narration_spans_the_whole_srt(self, minutes):
        segments = timeline(minutes)
        plan = build_plan(segments)
        window = GroupWindow(segments)
        covered = sum(window.target_ms(group) for group in plan)
        assert covered == segments[-1].end_ms - segments[0].start_ms

    def test_group_count_grows_with_duration(self):
        short = build_plan(timeline(2))
        long = build_plan(timeline(20))
        assert len(long) > len(short)

    def test_twenty_minutes_makes_roughly_the_expected_group_count(self):
        segments = timeline(20)
        plan = build_plan(segments)
        # 20 minutes at a 60 s cap needs at least 20 groups, and should not need
        # wildly more than that.
        assert 20 <= len(plan) <= 30

    def test_a_single_caption_longer_than_the_cap_is_still_generated(self):
        segments = [Segment(0, 90_000, "One very long uninterrupted caption.")]
        plan = build_plan(segments)
        assert len(plan) == 1
        assert GroupWindow(segments).target_ms(plan.groups[0]) == 90_000

    def test_exact_mode_has_no_length_limit_either(self):
        segments = timeline(20)
        plan = build_plan(segments, NarrationMode.EXACT)
        assert len(plan) == len(segments)


class TestCapIsACeilingNotATarget:
    def test_groups_stay_within_the_cap_plus_overflow(self):
        segments = timeline(20)
        plan = build_plan(segments)
        window = GroupWindow(segments)
        ceiling = DEFAULT_MAX_GROUP_MS + GroupingOptions().overflow_ms
        assert all(window.target_ms(group) <= ceiling for group in plan)

    def test_natural_break_ends_a_group_early(self):
        # A clear sentence end plus a long pause at 20 s must win over filling
        # the group to 60 s.
        segments = [
            Segment(0, 5000, "The first part continues onward with the"),
            Segment(5000, 10_000, "second part which finishes the thought here."),
            Segment(12_000, 17_000, "Now we begin a completely separate topic and"),
            Segment(17_000, 22_000, "it carries on to the end."),
        ]
        plan = build_plan(segments)
        window = GroupWindow(segments)
        assert len(plan) == 2
        assert window.end_ms(plan.groups[0]) == 10_000

    def test_overflow_is_taken_to_reach_a_better_boundary(self):
        # Boundary just past the cap is a sentence end; the ones inside are not.
        segments = [
            Segment(i * 10_000, (i + 1) * 10_000, "this clause continues with the")
            for i in range(6)
        ]
        segments[6 - 1] = Segment(
            50_000, 60_000, "and here the sentence finally ends.", uid=segments[5].uid
        )
        segments.append(Segment(60_000, 70_000, "A brand new topic starts here."))
        plan = build_plan(
            segments, options=GroupingOptions(max_group_ms=55_000, overflow_ms=12_000)
        )
        window = GroupWindow(segments)
        # 60 s > the 55 s cap, but it is the sentence end, so it is taken.
        assert window.end_ms(plan.groups[0]) == 60_000

    def test_overflow_is_not_taken_for_a_marginal_gain(self):
        segments = timeline(5)
        plan = build_plan(segments)
        window = GroupWindow(segments)
        # All boundaries score alike here, so nothing justifies overflowing.
        assert all(
            window.target_ms(group) <= DEFAULT_MAX_GROUP_MS for group in plan
        )


class TestFewestPossibleCuts:
    def test_greedy_beats_balanced_splitting_on_cut_count(self):
        # 5 minutes of uniform continuation at a 60 s cap needs 5 groups, not
        # the 8 that repeated halving would produce.
        segments = timeline(5)
        plan = build_plan(segments)
        assert len(plan) == 5

    def test_groups_fill_the_cap_when_no_boundary_is_better(self):
        segments = timeline(5)
        plan = build_plan(segments)
        window = GroupWindow(segments)
        # Every group except the last should be close to the cap.
        for group in list(plan)[:-1]:
            assert window.target_ms(group) > DEFAULT_MAX_GROUP_MS * 0.8
