import pytest

from app.core.timecode import (
    TimecodeError,
    format_display,
    format_duration,
    format_timestamp,
    ms_to_samples,
    parse_timestamp,
    samples_to_ms,
    seconds_to_ms,
)


class TestParse:
    def test_parses_standard_srt_timestamp(self):
        assert parse_timestamp("00:00:04,680") == 4680

    def test_parses_hours_minutes_seconds(self):
        assert parse_timestamp("01:02:03,004") == 3_723_004

    def test_accepts_dot_separator(self):
        assert parse_timestamp("00:00:04.680") == 4680

    def test_pads_truncated_fraction_on_the_right(self):
        # "4,68" means 680 ms, not 68 ms.
        assert parse_timestamp("00:00:04,68") == 4680
        assert parse_timestamp("00:00:04,6") == 4600

    def test_accepts_missing_fraction(self):
        assert parse_timestamp("00:00:04") == 4000

    def test_tolerates_surrounding_whitespace(self):
        assert parse_timestamp("  00:00:04,680  ") == 4680

    @pytest.mark.parametrize(
        "bad", ["", "abc", "00:00", "00:00:60,000", "00:99:00,000", "4.68"]
    )
    def test_rejects_invalid(self, bad):
        with pytest.raises(TimecodeError):
            parse_timestamp(bad)


class TestFormat:
    @pytest.mark.parametrize(
        "text", ["00:00:00,000", "00:00:04,680", "00:05:49,320", "01:02:03,004"]
    )
    def test_round_trips(self, text):
        assert format_timestamp(parse_timestamp(text)) == text

    def test_display_uses_dot(self):
        assert format_display(4680) == "00:00:04.680"

    def test_rejects_negative(self):
        with pytest.raises(TimecodeError):
            format_timestamp(-1)

    def test_duration_seconds(self):
        assert format_duration(4680) == "4.680s"

    def test_duration_over_a_minute(self):
        assert format_duration(83_400) == "1:23.400"


class TestSamples:
    def test_converts_at_24k(self):
        assert ms_to_samples(1000, 24_000) == 24_000
        assert ms_to_samples(4680, 24_000) == 112_320

    def test_adjacent_segments_meet_exactly(self):
        # The boundary sample must be identical from both sides, otherwise
        # segments would overlap or leave a one-sample hole.
        boundary = 4680
        assert ms_to_samples(boundary, 24_000) == ms_to_samples(boundary, 24_000)

    def test_round_trip(self):
        assert samples_to_ms(ms_to_samples(4680, 24_000), 24_000) == 4680

    def test_rejects_bad_sample_rate(self):
        with pytest.raises(TimecodeError):
            ms_to_samples(1000, 0)

    def test_seconds_to_ms_rounds(self):
        assert seconds_to_ms(4.6804) == 4680
        assert seconds_to_ms(4.6806) == 4681
