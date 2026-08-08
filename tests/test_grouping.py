"""Milestone 1: natural narration grouping."""

from pathlib import Path

import pytest

from app.core.models import Segment
from app.narration.groups import (
    GroupWindow,
    NarrationMode,
    SpeedSafety,
    build_narration_text,
    classify_speed,
)
from app.narration.grouping import (
    DEFAULT_MAX_GROUP_MS,
    GroupingOptions,
    analyse_all,
    analyse_boundary,
    build_plan,
    merge_groups,
    split_group,
)
from app.srt.parser import parse_srt

#: A transcript-shaped fixture that lives in the repo, so the suite runs for
#: anyone who clones it. Same characteristics as real Whisper output:
#: contiguous captions, wrapped mid-sentence, almost none ending a sentence.
TRANSCRIPT = Path(__file__).parent / "fixtures" / "transcript.srt"


def plan_for(segments, mode=NarrationMode.NATURAL, **kwargs):
    return build_plan(segments, mode, GroupingOptions(**kwargs))


class TestSpecCaseOne:
    """One sentence split across two captions must become ONE group."""

    def setup_method(self):
        self.segments = [
            Segment(0, 2000, "Welcome to PediAid, a platform built"),
            Segment(2000, 4000, "for pediatric and neonatal practice."),
        ]
        self.plan = plan_for(self.segments)

    def test_produces_one_group(self):
        assert len(self.plan) == 1

    def test_group_covers_both_captions(self):
        assert self.plan.groups[0].size == 2

    def test_narration_text_is_continuous(self):
        assert self.plan.groups[0].narration_text == (
            "Welcome to PediAid, a platform built for pediatric and neonatal practice."
        )

    def test_no_pause_at_two_seconds(self):
        # A single group is generated as one utterance, so nothing can insert a
        # boundary pause at 2 s.
        window = GroupWindow(self.segments)
        group = self.plan.groups[0]
        assert window.start_ms(group) == 0
        assert window.end_ms(group) == 4000

    def test_audio_window_ends_at_four_seconds(self):
        window = GroupWindow(self.segments)
        assert window.target_ms(self.plan.groups[0]) == 4000

    def test_captions_are_untouched(self):
        assert [(s.start_ms, s.end_ms) for s in self.segments] == [
            (0, 2000),
            (2000, 4000),
        ]


class TestSpecCaseTwo:
    """A finished sentence plus a real gap must become TWO groups."""

    def setup_method(self):
        self.segments = [
            Segment(0, 2000, "Welcome to PediAid."),
            Segment(4000, 6000, "Now let's look at the calculators."),
        ]
        self.plan = plan_for(self.segments)

    def test_produces_two_groups(self):
        assert len(self.plan) == 2

    def test_each_group_holds_one_caption(self):
        assert [g.size for g in self.plan.groups] == [1, 1]

    def test_two_second_gap_is_preserved(self):
        window = GroupWindow(self.segments)
        first, second = self.plan.groups
        assert window.end_ms(first) == 2000
        assert window.start_ms(second) == 4000
        assert window.start_ms(second) - window.end_ms(first) == 2000


class TestBoundarySignals:
    def test_dangling_preposition_joins(self):
        analysis = analyse_boundary(
            Segment(0, 2000, "a clinical reference platform built specifically for"),
            Segment(2000, 4000, "pediatrics and neonatology."),
            0,
            GroupingOptions(),
        )
        assert analysis.joins
        assert any("for" in reason for reason in analysis.join_reasons)

    def test_lowercase_start_joins(self):
        analysis = analyse_boundary(
            Segment(0, 2000, "The section contains a wide range of"),
            Segment(2000, 4000, "pediatric calculators."),
            0,
            GroupingOptions(),
        )
        assert analysis.joins

    def test_sentence_end_with_long_gap_breaks(self):
        analysis = analyse_boundary(
            Segment(0, 2000, "That is the summary."),
            Segment(4000, 6000, "Next we open the charts."),
            0,
            GroupingOptions(),
        )
        assert not analysis.joins

    def test_long_pause_breaks_even_without_punctuation(self):
        # Grammar says continue, but a two-second silence says the speaker stopped.
        analysis = analyse_boundary(
            Segment(0, 2000, "and the reference values"),
            Segment(4000, 6000, "are listed below"),
            0,
            GroupingOptions(),
        )
        assert analysis.gap_ms == 2000
        assert any("pause" in reason for reason in analysis.break_reasons)

    def test_abbreviation_is_not_a_sentence_end(self):
        analysis = analyse_boundary(
            Segment(0, 2000, "The dose was confirmed by Dr."),
            Segment(2000, 4000, "Rao before administration."),
            0,
            GroupingOptions(),
        )
        assert analysis.joins

    def test_decimal_is_not_a_sentence_end(self):
        analysis = analyse_boundary(
            Segment(0, 2000, "The threshold is 0.5"),
            Segment(2000, 4000, "millimoles per litre."),
            0,
            GroupingOptions(),
        )
        assert analysis.joins

    def test_comma_ending_joins(self):
        analysis = analyse_boundary(
            Segment(0, 2000, "growth charts, drug references,"),
            Segment(2000, 4000, "laboratory values"),
            0,
            GroupingOptions(),
        )
        assert analysis.joins

    def test_topic_marker_breaks(self):
        analysis = analyse_boundary(
            Segment(0, 2000, "That completes the calculators."),
            Segment(2000, 4000, "Let's start with the charts."),
            0,
            GroupingOptions(),
        )
        assert not analysis.joins

    def test_score_is_clamped(self):
        for analysis in analyse_all(
            [
                Segment(0, 2000, "built specifically for"),
                Segment(2000, 4000, "pediatrics and neonatology"),
            ]
        ):
            assert 0.0 <= analysis.score <= 1.0

    def test_explanation_is_human_readable(self):
        analysis = analyse_boundary(
            Segment(0, 2000, "built specifically for"),
            Segment(2000, 4000, "pediatrics."),
            0,
            GroupingOptions(),
        )
        assert analysis.explanation
        assert "score" not in analysis.explanation.lower()


class TestExactMode:
    def test_one_group_per_caption(self):
        segments = [
            Segment(0, 2000, "Welcome to PediAid, a platform built"),
            Segment(2000, 4000, "for pediatric and neonatal practice."),
        ]
        plan = build_plan(segments, NarrationMode.EXACT)
        assert len(plan) == 2
        assert all(group.is_single for group in plan)

    def test_reproduces_the_original_behaviour_on_the_real_file(self):
        segments = parse_srt(TRANSCRIPT.read_text()).segments
        plan = build_plan(segments, NarrationMode.EXACT)
        assert len(plan) == len(segments)


class TestLengthCap:
    def _long_run(self, count=12, each_ms=5000):
        # Every caption ends on a dangling word, so all boundaries want to join.
        return [
            Segment(i * each_ms, (i + 1) * each_ms, f"part {i} continues with the")
            for i in range(count)
        ]

    def test_uncapped_signals_would_join_everything(self):
        segments = self._long_run()
        assert all(a.joins for a in analyse_all(segments))

    def test_cap_splits_over_long_runs(self):
        plan = plan_for(self._long_run(), max_group_ms=30_000, max_group_segments=99)
        assert len(plan) > 1

    def test_no_group_exceeds_the_duration_cap(self):
        segments = self._long_run()
        plan = plan_for(segments, max_group_ms=30_000, max_group_segments=99)
        window = GroupWindow(segments)
        assert all(window.target_ms(group) <= 30_000 for group in plan)

    def test_no_group_exceeds_the_segment_cap(self):
        plan = plan_for(self._long_run(20), max_group_ms=10**9, max_group_segments=4)
        assert all(group.size <= 4 for group in plan)

    def test_cap_does_not_shave_off_single_captions(self):
        # Centrality bias should produce balanced splits, not 1 + many.
        plan = plan_for(self._long_run(12), max_group_ms=30_000, max_group_segments=99)
        assert min(group.size for group in plan) > 1


class TestCoverage:
    """Whatever the mode, every caption must belong to exactly one group."""

    @pytest.fixture
    def segments(self):
        return parse_srt(TRANSCRIPT.read_text()).segments

    @pytest.mark.parametrize("mode", [NarrationMode.NATURAL, NarrationMode.EXACT])
    def test_every_caption_appears_once(self, segments, mode):
        plan = build_plan(segments, mode)
        uids = [uid for group in plan for uid in group.segment_uids]
        assert uids == [segment.uid for segment in segments]

    def test_groups_are_contiguous_and_ordered(self, segments):
        plan = build_plan(segments)
        window = GroupWindow(segments)
        for group in plan:
            indices = window.indices_of(group)
            assert indices == list(range(indices[0], indices[-1] + 1))

    def test_groups_do_not_overlap(self, segments):
        plan = build_plan(segments)
        window = GroupWindow(segments)
        ends = [window.end_ms(g) for g in plan]
        starts = [window.start_ms(g) for g in plan]
        for previous_end, next_start in zip(ends, starts[1:]):
            assert next_start >= previous_end


class TestRealFile:
    """Regression checks against PediAid_narration_enhanced.srt."""

    @pytest.fixture
    def segments(self):
        return parse_srt(TRANSCRIPT.read_text()).segments

    def test_captions_are_contiguous_like_a_real_transcript(self, segments):
        assert len(segments) > 20
        for previous, following in zip(segments, segments[1:]):
            assert following.start_ms == previous.end_ms

    def test_timestamps_unchanged_by_grouping(self, segments):
        before = [(s.start_ms, s.end_ms) for s in segments]
        build_plan(segments)
        assert [(s.start_ms, s.end_ms) for s in segments] == before

    def test_grouping_reduces_the_number_of_utterances(self, segments):
        plan = build_plan(segments)
        assert 1 < len(plan) < len(segments)

    def test_timeline_is_fully_covered(self, segments):
        plan = build_plan(segments)
        window = GroupWindow(segments)
        assert window.start_ms(plan.groups[0]) == segments[0].start_ms
        assert window.end_ms(plan.groups[-1]) == segments[-1].end_ms

    def test_a_sentence_spanning_two_captions_is_one_utterance(self, segments):
        # The headline requirement: caption 1 does not end a sentence, so
        # captions 1 and 2 must be spoken continuously.
        plan = build_plan(segments)
        group = plan.group_for_segment(segments[0].uid)
        assert segments[1].uid in group.segment_uids
        joined = f"{segments[0].text.strip()} {segments[1].text.strip()}"
        assert joined in group.narration_text

    def test_narration_text_has_no_boundary_artefacts(self, segments):
        plan = build_plan(segments)
        for group in plan:
            assert "  " not in group.narration_text
            assert group.narration_text == group.narration_text.strip()

    def test_no_group_exceeds_the_cap_plus_its_overflow_allowance(self, segments):
        # The cap is a ceiling to aim under, not a hard wall: a group may run
        # slightly past it to finish on a better boundary.
        plan = build_plan(segments)
        window = GroupWindow(segments)
        ceiling = DEFAULT_MAX_GROUP_MS + GroupingOptions().overflow_ms
        assert all(window.target_ms(group) <= ceiling for group in plan)

    def test_no_group_ends_on_an_unfinished_phrase(self, segments):
        # The whole point of the exercise: a cut must not land on "…for the".
        from app.narration.report import preview_plan

        rows = preview_plan(build_plan(segments), segments)
        unfinished = [row for row in rows if not row.natural_boundary]
        assert not unfinished, [row.ends_on for row in unfinished]

    def test_no_stub_groups(self, segments):
        plan = build_plan(segments)
        window = GroupWindow(segments)
        # Only the final group may be short; a cap-driven cut must not carve
        # off a few seconds and add a needless join.
        for group in list(plan)[:-1]:
            assert window.target_ms(group) >= GroupingOptions().min_group_ms

    def test_forced_cuts_are_flagged_not_hidden(self, segments):
        # Whisper wraps lines mid-sentence, so the cap has to cut mid-sentence
        # too. Those groups must be reported rather than silently accepted.
        plan = build_plan(segments)
        forced = [g for g in plan if g.forced_cut]
        assert forced
        assert not plan.groups[0].forced_cut  # the first group never is

    def test_natural_breaks_are_not_flagged_as_forced(self, segments):
        # With no cap at all, every remaining break is one the signals asked for.
        plan = build_plan(
            segments, options=GroupingOptions(max_group_ms=10**9, max_group_segments=10**6)
        )
        assert not any(group.forced_cut for group in plan)


class TestNarrationText:
    def test_joins_with_a_single_space(self):
        segments = [Segment(0, 1000, "Hello  there"), Segment(1000, 2000, "  world ")]
        assert build_narration_text(segments) == "Hello there world"

    def test_skips_empty_captions(self):
        segments = [Segment(0, 1000, "Hello"), Segment(1000, 2000, "   ")]
        assert build_narration_text(segments) == "Hello"

    def test_custom_text_wins_over_captions(self):
        segments = [Segment(0, 1000, "Original text")]
        plan = build_plan(segments)
        group = plan.groups[0]
        group.narration_text = "Spoken differently"
        group.text_is_custom = True
        assert GroupWindow(segments).narration_text(group) == "Spoken differently"

    def test_caption_text_is_not_overwritten_by_narration(self):
        segments = [
            Segment(0, 2000, "Welcome to PediAid, a platform built"),
            Segment(2000, 4000, "for pediatric and neonatal practice."),
        ]
        build_plan(segments)
        assert segments[0].text == "Welcome to PediAid, a platform built"
        assert segments[0].caption_text == segments[0].text


class TestManualEditing:
    @pytest.fixture
    def setup(self):
        segments = [
            Segment(0, 2000, "one continues with the"),
            Segment(2000, 4000, "second part and the"),
            Segment(4000, 6000, "third part finishes here."),
        ]
        return segments, build_plan(segments)

    def test_auto_grouped_into_one(self, setup):
        _, plan = setup
        assert len(plan) == 1

    def test_split_produces_two_groups(self, setup):
        segments, plan = setup
        updated = split_group(plan, 0, after_member=0, segments=segments)
        assert len(updated) == 2
        assert [g.size for g in updated] == [1, 2]

    def test_split_switches_mode_to_manual(self, setup):
        segments, plan = setup
        assert split_group(plan, 0, 0, segments).mode is NarrationMode.MANUAL

    def test_split_rebuilds_narration_text(self, setup):
        segments, plan = setup
        updated = split_group(plan, 0, 0, segments)
        assert updated.groups[0].narration_text == "one continues with the"

    def test_split_rejects_a_point_outside_the_group(self, setup):
        segments, plan = setup
        with pytest.raises(ValueError):
            split_group(plan, 0, after_member=5, segments=segments)

    def test_merge_recombines(self, setup):
        segments, plan = setup
        split = split_group(plan, 0, 0, segments)
        merged = merge_groups(split, [0, 1], segments)
        assert len(merged) == 1
        assert merged.groups[0].size == 3

    def test_merge_rejects_non_consecutive(self, setup):
        segments, _ = setup
        exact = build_plan(segments, NarrationMode.EXACT)
        with pytest.raises(ValueError):
            merge_groups(exact, [0, 2], segments)

    def test_original_plan_is_not_mutated(self, setup):
        segments, plan = setup
        split_group(plan, 0, 0, segments)
        assert len(plan) == 1


class TestSpeedSafety:
    @pytest.mark.parametrize(
        "factor,expected",
        [
            (0.80, SpeedSafety.SAFE),
            (1.00, SpeedSafety.SAFE),
            (1.08, SpeedSafety.SAFE),
            (1.09, SpeedSafety.WARNING),
            (1.15, SpeedSafety.WARNING),
            (1.16, SpeedSafety.STRONG_WARNING),
            (1.30, SpeedSafety.STRONG_WARNING),
            (1.45, SpeedSafety.NEEDS_CONFIRMATION),
        ],
    )
    def test_thresholds(self, factor, expected):
        assert classify_speed(factor) is expected


class TestGroupWindow:
    def test_window_comes_from_captions_not_audio(self):
        segments = [Segment(0, 2000, "a"), Segment(2000, 4000, "b")]
        plan = build_plan(segments)
        window = GroupWindow(segments)
        group = plan.groups[0]
        # No audio has been generated at all, yet the window is fully defined.
        assert group.audio is None
        assert (window.start_ms(group), window.end_ms(group)) == (0, 4000)

    def test_window_follows_caption_edits(self):
        segments = [Segment(0, 2000, "one continues with the"), Segment(2000, 4000, "two.")]
        plan = build_plan(segments)
        segments[1] = Segment(
            2000, 9000, "two.", uid=segments[1].uid
        )
        assert GroupWindow(segments).end_ms(plan.groups[0]) == 9000

    def test_empty_group_is_safe(self):
        from app.narration.groups import NarrationGroup

        window = GroupWindow([])
        assert window.target_ms(NarrationGroup(segment_uids=["missing"])) == 0
