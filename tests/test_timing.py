"""Fitting speech to its window, and the anti-drift placement rule."""

import numpy as np
import pytest

from app.audio.assemble import DEFAULT_SAMPLE_RATE, PlacedGroup, assemble
from app.audio.timing import (
    FillPolicy,
    FitAction,
    FitOptions,
    atempo_chain,
    atempo_filter,
    plan_fit,
)
from app.core.timecode import ms_to_samples
from app.narration.groups import SpeedSafety

PAD_ONLY = FitOptions(fill_policy=FillPolicy.PAD_ONLY)


class TestAtempoChain:
    def test_identity(self):
        assert atempo_chain(1.0) == ["atempo=1.00000000"]

    def test_within_range_is_one_stage(self):
        assert len(atempo_chain(1.45)) == 1

    def test_above_two_is_chained(self):
        stages = atempo_chain(4.0)
        assert stages[0] == "atempo=2.00000000"
        assert len(stages) == 2

    def test_below_half_is_chained(self):
        assert len(atempo_chain(0.25)) == 2

    def test_chain_multiplies_back_to_the_factor(self):
        for factor in (0.3, 0.5, 0.9, 1.0, 1.45, 2.0, 3.7, 8.0):
            product = 1.0
            for stage in atempo_chain(factor):
                product *= float(stage.split("=")[1])
            assert product == pytest.approx(factor, rel=1e-6)

    def test_every_stage_is_legal_for_ffmpeg(self):
        for factor in (0.1, 0.4, 1.0, 5.0, 16.0):
            for stage in atempo_chain(factor):
                value = float(stage.split("=")[1])
                assert 0.5 <= value <= 2.0 + 1e-9

    def test_filter_is_comma_joined(self):
        assert atempo_filter(4.0) == "atempo=2.00000000,atempo=2.00000000"

    def test_rejects_non_positive(self):
        with pytest.raises(ValueError):
            atempo_chain(0)


class TestCompression:
    def test_longer_speech_is_compressed_not_truncated(self):
        plan = plan_fit(generated_ms=5800, target_ms=4000)
        assert plan.action is FitAction.COMPRESS
        assert plan.speed_factor == pytest.approx(1.45)
        assert plan.fitted_ms == 4000

    def test_compression_never_pads(self):
        assert plan_fit(5800, 4000).pad_ms == 0

    def test_extreme_compression_needs_confirmation(self):
        plan = plan_fit(5800, 4000)
        assert plan.safety is SpeedSafety.NEEDS_CONFIRMATION
        assert plan.needs_confirmation

    def test_mild_compression_is_applied_silently(self):
        plan = plan_fit(4200, 4000)
        assert plan.safety is SpeedSafety.SAFE
        assert not plan.needs_confirmation

    def test_confirmation_can_be_waived(self):
        options = FitOptions(allow_extreme_compression=True)
        assert not plan_fit(5800, 4000, options).needs_confirmation

    def test_result_always_fits_the_window(self):
        for generated in (100, 3999, 4000, 4001, 9000):
            assert plan_fit(generated, 4000).fits


class TestFill:
    def test_pad_only_matches_the_proof_of_concept(self):
        plan = plan_fit(3200, 4000, PAD_ONLY)
        assert plan.action is FitAction.PAD
        assert plan.speed_factor == 1.0
        assert plan.pad_ms == 800
        assert plan.fitted_ms == 3200

    def test_stretch_fills_a_small_shortfall_completely(self):
        plan = plan_fit(3800, 4000)
        assert plan.action is FitAction.STRETCH
        assert plan.pad_ms == 0
        assert plan.final_ms == 4000

    def test_moderate_shortfall_is_stretched_not_padded(self):
        # 8.0s of speech in an 8.7s window: exactly the 2:43 failure shape.
        # It must close the gap by slowing down, never by inserting silence.
        plan = plan_fit(8000, 8720)
        assert plan.pad_ms == 0
        assert plan.silence_inserted_ms == 0
        assert plan.final_ms == 8720

    def test_large_shortfall_stretches_to_the_hard_floor_first(self):
        plan = plan_fit(2000, 4000)
        assert plan.action is FitAction.STRETCH_AND_PAD
        assert plan.speed_factor == pytest.approx(0.75)
        assert plan.pad_ms > 0

    def test_stretch_never_goes_below_the_hard_limit(self):
        assert plan_fit(1000, 10_000).speed_factor >= 0.75

    def test_multi_second_silence_is_flagged_as_unnatural(self):
        plan = plan_fit(2000, 12_000)
        assert plan.unnatural_silence
        assert "Merge this group" in plan.message

    def test_small_trailing_silence_is_not_flagged(self):
        assert not plan_fit(8000, 8720).unnatural_silence

    def test_a_five_second_window_with_two_seconds_of_speech_is_flagged(self):
        # The exact PoC failure: it padded ~3.7s. Now it stretches as far as it
        # can and reports the rest instead of silently producing a hole.
        plan = plan_fit(2000, 5720)
        assert plan.unnatural_silence
        assert plan.speed_factor == pytest.approx(0.75)

    def test_slowing_down_is_always_safe(self):
        assert plan_fit(2000, 4000).safety is SpeedSafety.SAFE

    def test_silence_only_goes_at_the_end(self):
        # The plan exposes one pad value, appended after the speech. There is no
        # mechanism to insert silence inside a group.
        plan = plan_fit(2000, 4000, PAD_ONLY)
        assert plan.pad_ms == 2000
        assert plan.fitted_ms == 2000


class TestEdgeCases:
    def test_negligible_difference_is_left_alone(self):
        plan = plan_fit(4005, 4000)
        assert plan.action is FitAction.NONE
        assert plan.speed_factor == 1.0

    def test_no_audio_becomes_silence(self):
        plan = plan_fit(0, 4000)
        assert plan.pad_ms == 4000
        assert plan.action is FitAction.PAD

    def test_zero_window_produces_nothing(self):
        plan = plan_fit(4000, 0)
        assert plan.final_ms == 0

    def test_negative_window_is_handled(self):
        assert plan_fit(4000, -100).target_ms == 0


class TestAssemblyPlacement:
    """The anti-drift guarantee, verified on real buffers."""

    def _tone(self, ms, rate=DEFAULT_SAMPLE_RATE, value=0.5):
        return np.full(ms_to_samples(ms, rate), value, dtype=np.float32)

    def test_group_lands_at_its_exact_start_sample(self):
        placed = [
            PlacedGroup(0, 10_000, 14_000, self._tone(4000), plan_fit(4000, 4000))
        ]
        audio, _ = assemble(placed, timeline_ms=20_000, crossfade_ms=0)
        start = ms_to_samples(10_000, DEFAULT_SAMPLE_RATE)
        assert audio[start - 1] == 0.0
        assert audio[start] != 0.0

    def test_timeline_length_comes_from_the_srt_not_the_audio(self):
        placed = [PlacedGroup(0, 0, 4000, self._tone(4000), plan_fit(4000, 4000))]
        audio, report = assemble(placed, timeline_ms=349_320, crossfade_ms=0)
        assert len(audio) == ms_to_samples(349_320, DEFAULT_SAMPLE_RATE)
        assert report.timeline_ms == 349_320

    def test_a_short_group_does_not_shift_the_next_one(self):
        # Group 0 renders 1 s short. Group 1 must still start exactly at 10 s.
        placed = [
            PlacedGroup(0, 0, 10_000, self._tone(9000), plan_fit(9000, 10_000)),
            PlacedGroup(1, 10_000, 20_000, self._tone(10_000), plan_fit(10_000, 10_000)),
        ]
        audio, _ = assemble(placed, timeline_ms=20_000, crossfade_ms=0)
        boundary = ms_to_samples(10_000, DEFAULT_SAMPLE_RATE)
        silence_start = ms_to_samples(9000, DEFAULT_SAMPLE_RATE)
        assert audio[silence_start + 100] == 0.0   # the shortfall is silence
        assert audio[boundary] != 0.0              # and group 1 is still on time

    def test_no_cumulative_drift_across_many_groups(self):
        placed = [
            PlacedGroup(
                i, i * 5000, (i + 1) * 5000,
                self._tone(4800), plan_fit(4800, 5000),
            )
            for i in range(60)
        ]
        audio, _ = assemble(placed, timeline_ms=300_000, crossfade_ms=0)
        for i in range(60):
            start = ms_to_samples(i * 5000, DEFAULT_SAMPLE_RATE)
            assert audio[start] != 0.0, f"group {i} did not start on time"

    def test_groups_do_not_overwrite_each_other(self):
        placed = [
            PlacedGroup(0, 0, 5000, self._tone(5000, value=0.5), plan_fit(5000, 5000)),
            PlacedGroup(1, 5000, 10_000, self._tone(5000, value=0.25), plan_fit(5000, 5000)),
        ]
        audio, _ = assemble(placed, timeline_ms=10_000, crossfade_ms=0, peak=1.0)
        first = ms_to_samples(2500, DEFAULT_SAMPLE_RATE)
        second = ms_to_samples(7500, DEFAULT_SAMPLE_RATE)
        assert audio[first] == pytest.approx(0.5, abs=1e-6)
        assert audio[second] == pytest.approx(0.25, abs=1e-6)

    def test_overflowing_audio_is_clipped_to_the_timeline_and_reported(self):
        placed = [PlacedGroup(0, 0, 5000, self._tone(8000), plan_fit(8000, 8000))]
        audio, report = assemble(placed, timeline_ms=5000, crossfade_ms=0)
        assert len(audio) == ms_to_samples(5000, DEFAULT_SAMPLE_RATE)
        assert report.issues

    def test_gaps_between_groups_stay_silent(self):
        placed = [
            PlacedGroup(0, 0, 2000, self._tone(2000), plan_fit(2000, 2000)),
            PlacedGroup(1, 4000, 6000, self._tone(2000), plan_fit(2000, 2000)),
        ]
        audio, _ = assemble(placed, timeline_ms=6000, crossfade_ms=0)
        middle = ms_to_samples(3000, DEFAULT_SAMPLE_RATE)
        assert audio[middle] == 0.0


class TestNormalisation:
    def _tone(self, ms, value):
        return np.full(ms_to_samples(ms, DEFAULT_SAMPLE_RATE), value, dtype=np.float32)

    def test_loud_audio_is_brought_down_to_the_headroom_target(self):
        placed = [PlacedGroup(0, 0, 1000, self._tone(1000, 1.0), plan_fit(1000, 1000))]
        audio, report = assemble(placed, timeline_ms=1000, peak=0.92, crossfade_ms=0)
        assert float(np.max(np.abs(audio))) == pytest.approx(0.92, abs=1e-4)
        assert report.gain_applied < 1.0

    def test_quiet_audio_is_not_amplified(self):
        placed = [PlacedGroup(0, 0, 1000, self._tone(1000, 0.2), plan_fit(1000, 1000))]
        audio, report = assemble(placed, timeline_ms=1000, peak=0.92, crossfade_ms=0)
        assert report.gain_applied == 1.0
        assert float(np.max(np.abs(audio))) == pytest.approx(0.2, abs=1e-4)

    def test_output_never_clips(self):
        placed = [PlacedGroup(0, 0, 1000, self._tone(1000, 3.0), plan_fit(1000, 1000))]
        audio, _ = assemble(placed, timeline_ms=1000, peak=0.92, crossfade_ms=0)
        assert float(np.max(np.abs(audio))) <= 1.0


class TestCrossfade:
    def _tone(self, ms, value=0.5):
        return np.full(ms_to_samples(ms, DEFAULT_SAMPLE_RATE), value, dtype=np.float32)

    def test_crossfade_does_not_change_the_timeline_length(self):
        placed = [
            PlacedGroup(0, 0, 5000, self._tone(5000), plan_fit(5000, 5000)),
            PlacedGroup(1, 5000, 10_000, self._tone(5000), plan_fit(5000, 5000)),
        ]
        audio, _ = assemble(placed, timeline_ms=10_000, crossfade_ms=40)
        assert len(audio) == ms_to_samples(10_000, DEFAULT_SAMPLE_RATE)

    def test_crossfade_only_applies_where_groups_actually_touch(self):
        placed = [
            PlacedGroup(0, 0, 2000, self._tone(2000), plan_fit(2000, 2000)),
            PlacedGroup(1, 4000, 6000, self._tone(2000), plan_fit(2000, 2000)),
        ]
        _, report = assemble(placed, timeline_ms=6000, crossfade_ms=40)
        assert report.crossfades_applied == 0

    def test_crossfade_is_capped(self):
        placed = [
            PlacedGroup(0, 0, 5000, self._tone(5000), plan_fit(5000, 5000)),
            PlacedGroup(1, 5000, 10_000, self._tone(5000), plan_fit(5000, 5000)),
        ]
        # A wild request must not overlap words; it is clamped to the maximum.
        audio, report = assemble(placed, timeline_ms=10_000, crossfade_ms=5000)
        assert report.crossfades_applied == 1
        assert len(audio) == ms_to_samples(10_000, DEFAULT_SAMPLE_RATE)

    def test_zero_disables_it(self):
        placed = [
            PlacedGroup(0, 0, 5000, self._tone(5000), plan_fit(5000, 5000)),
            PlacedGroup(1, 5000, 10_000, self._tone(5000), plan_fit(5000, 5000)),
        ]
        _, report = assemble(placed, timeline_ms=10_000, crossfade_ms=0)
        assert report.crossfades_applied == 0
