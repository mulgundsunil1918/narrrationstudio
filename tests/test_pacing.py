"""Pacing: the voice should sound like a person, not a tape being varispeeded.

The complaint that produced this feature: with AI-guessed caption timings, the
same narration drawled in one group (slowed to 0.75× to fill a long window)
and gabbled in the next (squeezed 1.3× into a short one). The cure is to bend
the voice less and bend the *silence* more — a long sentence may finish in the
quiet gap after its caption, a short one leaves quiet rather than being
dragged out. The captions themselves never move.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.audio.timing import (
    FitAction,
    FitOptions,
    PACING_PRESETS,
    fit_options_for,
    plan_fit,
)


# -- borrowing the gap ---------------------------------------------------


class TestGapBorrowing:
    def test_borrowing_slows_the_rush(self):
        """6s of speech in a 4s window with 3s of gap: barely sped up at all."""
        options = FitOptions(gap_borrow_fraction=1.0)
        plan = plan_fit(6000, 4000, options, available_ms=7000)

        assert plan.spill_ms == 2000                    # finished in the gap
        assert plan.speed_factor == pytest.approx(1.0)  # at natural pace
        assert plan.final_ms == 6000

    def test_partial_borrowing_splits_the_difference(self):
        options = FitOptions(gap_borrow_fraction=0.5)
        plan = plan_fit(6000, 4000, options, available_ms=8000)

        # Half of the 4s gap may be borrowed: 2s. Speech compresses into 6s.
        assert plan.spill_ms == 2000
        assert plan.speed_factor == pytest.approx(1.0)

    def test_spill_can_never_reach_the_next_group(self):
        """The invariant that makes borrowing safe at all."""
        options = FitOptions(gap_borrow_fraction=1.0)
        for generated in (5000, 9000, 20_000):
            plan = plan_fit(generated, 4000, options, available_ms=7000)
            assert plan.target_ms + plan.spill_ms <= 7000
            assert plan.final_ms <= 7000

    def test_no_gap_means_no_borrowing(self):
        """Groups that touch: the old exact behaviour, unchanged."""
        options = FitOptions(gap_borrow_fraction=1.0)
        plan = plan_fit(6000, 4000, options, available_ms=4000)
        assert plan.spill_ms == 0
        assert plan.speed_factor == pytest.approx(1.5)

    def test_zero_fraction_is_the_old_behaviour(self):
        with_gap = plan_fit(6000, 4000, FitOptions(gap_borrow_fraction=0.0),
                            available_ms=10_000)
        without = plan_fit(6000, 4000, FitOptions())
        assert with_gap.speed_factor == without.speed_factor == pytest.approx(1.5)
        assert with_gap.spill_ms == 0

    def test_no_available_room_given_means_no_borrowing(self):
        plan = plan_fit(6000, 4000, FitOptions(gap_borrow_fraction=1.0))
        assert plan.spill_ms == 0

    def test_short_speech_never_spills(self):
        """Borrowing is for overflow only; padding stays inside the window."""
        plan = plan_fit(2000, 4000, FitOptions(gap_borrow_fraction=1.0),
                        available_ms=10_000)
        assert plan.spill_ms == 0
        assert plan.final_ms <= 4000


# -- the presets ---------------------------------------------------------


class TestPacingPresets:
    def test_the_three_presets_exist_and_unknown_falls_back(self):
        assert set(PACING_PRESETS) == {"exact", "balanced", "natural"}
        assert fit_options_for("nonsense") == PACING_PRESETS["balanced"]

    def test_exact_is_the_original_behaviour(self):
        assert fit_options_for("exact") == FitOptions()

    def test_natural_barely_slows_the_voice(self):
        """The 'says it very slowly' complaint: capped at a few percent."""
        options = fit_options_for("natural")
        plan = plan_fit(2000, 6000, options)     # a third of the window

        assert plan.speed_factor >= 0.97
        assert plan.pad_ms > 0                   # quiet, instead of drawling
        assert plan.action in (FitAction.STRETCH_AND_PAD, FitAction.PAD)

    def test_exact_still_drawls_where_told_to(self):
        plan = plan_fit(4000, 5000, fit_options_for("exact"))
        assert plan.speed_factor == pytest.approx(0.8, abs=0.01)
        assert plan.pad_ms == 0

    def test_balanced_pads_rather_than_drawling_deeply(self):
        plan = plan_fit(3000, 6000, fit_options_for("balanced"))
        assert plan.speed_factor >= 0.88
        assert plan.pad_ms > 0

    def test_balanced_borrows_before_rushing(self):
        options = fit_options_for("balanced")
        strict = plan_fit(6000, 4000, fit_options_for("exact"), available_ms=8000)
        eased = plan_fit(6000, 4000, options, available_ms=8000)
        assert strict.speed_factor == pytest.approx(1.5)
        assert eased.speed_factor < 1.1


# -- through the audio path ----------------------------------------------


class TestFitAudioWithSpill:
    def _tone(self, ms: int, rate: int = 24_000) -> np.ndarray:
        return np.full(int(rate * ms / 1000), 0.3, dtype=np.float32)

    def test_spilled_audio_keeps_its_length(self):
        from app.audio.assemble import fit_audio

        audio = self._tone(6000)
        fitted, plan = fit_audio(
            audio, 4000, 24_000, FitOptions(gap_borrow_fraction=1.0),
            available_ms=7000,
        )
        assert plan.spill_ms == 2000
        assert len(fitted) == pytest.approx(24_000 * 6, rel=0.01)

    def test_without_borrowing_the_window_is_still_the_wall(self):
        from app.audio.assemble import fit_audio

        audio = self._tone(6000)
        fitted, plan = fit_audio(audio, 4000, 24_000, FitOptions())
        assert len(fitted) == 24_000 * 4

    def test_spill_lands_in_the_gap_not_on_the_next_group(self):
        """Assembled end to end: the borrowed tail sits in silence."""
        from app.audio.assemble import PlacedGroup, assemble, fit_audio
        from app.core.timecode import ms_to_samples

        rate = 24_000
        first, plan_a = fit_audio(
            self._tone(6000), 4000, rate,
            FitOptions(gap_borrow_fraction=1.0), available_ms=7000,
        )
        second, plan_b = fit_audio(self._tone(3000), 3000, rate, FitOptions())

        timeline, report = assemble(
            [
                PlacedGroup(0, 0, 4000, first, plan_a),
                PlacedGroup(1, 7000, 10_000, second, plan_b),
            ],
            timeline_ms=10_000, crossfade_ms=0,
        )
        assert not report.issues
        # 4s–6s: the borrowed tail, audible.
        assert np.abs(timeline[ms_to_samples(4500, rate)]) > 0.01
        # 6s–7s: unborrowed gap, silent.
        assert np.abs(timeline[ms_to_samples(6500, rate)]) < 1e-6
        # 7s on: the second group, untouched by the spill.
        assert np.abs(timeline[ms_to_samples(7500, rate)]) > 0.01


# -- the whole pipeline --------------------------------------------------


def test_pipeline_passes_the_gap_to_the_fitter(monkeypatch):
    """Two captions with a gap: long speech must borrow it, not rush."""
    from app.core.models import Segment
    from app.pipeline import GenerationSettings, generate
    from app.tts.base import GenerationResult
    from app.tts.registry import engine as get_engine

    rate = 24_000

    def slow_speaker(request):
        # Whatever the text, the voice takes 6 seconds to say it.
        return GenerationResult(
            np.full(rate * 6, 0.2, dtype=np.float32), rate, 6000,
            "kokoro", request.voice,
        )

    backend = get_engine("kokoro")
    monkeypatch.setattr(backend, "generate", slow_speaker)
    monkeypatch.setattr(backend, "is_available", lambda: (True, ""))

    segments = [
        Segment(0, 4000, "A sentence that takes longer than its window."),
        Segment(7000, 10_000, "A later caption, after a three-second gap."),
    ]
    outcome = generate(
        segments,
        GenerationSettings(
            use_cache=False,
            fit=fit_options_for("balanced"),
        ),
    )
    assert not outcome.failures
    first_plan = outcome.fit_plans[0]
    assert first_plan.spill_ms > 0, "the gap was there to borrow and was not"
    assert first_plan.speed_factor < 1.2
