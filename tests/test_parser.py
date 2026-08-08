from pathlib import Path

import pytest

from app.core.errors import FileFormatError, UnsupportedFileError
from app.srt.parser import load, parse_plain_text, parse_srt

FIXTURE = Path(__file__).parent / "fixtures" / "sample.srt"


class TestSpecFixture:
    """The exact verification from §34."""

    @pytest.fixture
    def segments(self):
        return parse_srt(FIXTURE.read_text()).segments

    def test_reads_all_three(self, segments):
        assert len(segments) == 3

    def test_timings_are_exact(self, segments):
        assert [(s.start_ms, s.end_ms) for s in segments] == [
            (0, 2000),
            (3000, 5000),
            (5000, 8000),
        ]

    def test_text_is_exact(self, segments):
        assert [s.text for s in segments] == [
            "Hello PediAid.",
            "Clinical tools in one place.",
            "Built for pediatric practice.",
        ]

    def test_one_second_gap_after_segment_one(self, segments):
        assert segments[1].start_ms - segments[0].end_ms == 1000

    def test_no_gap_between_two_and_three(self, segments):
        assert segments[2].start_ms - segments[1].end_ms == 0

    def test_no_overlap(self, segments):
        for previous, following in zip(segments, segments[1:]):
            assert following.start_ms >= previous.end_ms

    def test_durations(self, segments):
        assert [s.duration_ms for s in segments] == [2000, 2000, 3000]

    def test_final_timeline_duration(self, segments):
        assert max(s.end_ms for s in segments) == 8000


class TestTolerance:
    def test_handles_crlf(self):
        content = "1\r\n00:00:00,000 --> 00:00:02,000\r\nHello.\r\n"
        assert parse_srt(content).segments[0].text == "Hello."

    def test_handles_bom(self):
        content = "﻿1\n00:00:00,000 --> 00:00:02,000\nHello.\n"
        assert len(parse_srt(content).segments) == 1

    def test_handles_missing_index_line(self):
        content = "00:00:00,000 --> 00:00:02,000\nHello.\n"
        assert parse_srt(content).segments[0].start_ms == 0

    def test_joins_multi_line_text(self):
        content = "1\n00:00:00,000 --> 00:00:02,000\nHello\nthere.\n"
        assert parse_srt(content).segments[0].text == "Hello there."

    def test_accepts_dot_separator(self):
        content = "1\n00:00:00.000 --> 00:00:02.000\nHello.\n"
        assert parse_srt(content).segments[0].end_ms == 2000

    def test_ignores_trailing_position_data(self):
        content = (
            "1\n00:00:00,000 --> 00:00:02,000 X1:100 X2:200 Y1:10 Y2:20\nHello.\n"
        )
        assert parse_srt(content).segments[0].end_ms == 2000

    def test_skips_malformed_block_with_warning(self):
        content = (
            "1\n00:00:00,000 --> 00:00:02,000\nGood.\n\n"
            "2\nnot a timing line\nBad.\n\n"
            "3\n00:00:03,000 --> 00:00:04,000\nAlso good.\n"
        )
        result = parse_srt(content)
        assert len(result.segments) == 2
        assert any("skipped" in w for w in result.warnings)

    def test_swaps_reversed_timestamps_and_warns(self):
        content = "1\n00:00:05,000 --> 00:00:02,000\nBackwards.\n"
        result = parse_srt(content)
        assert (result.segments[0].start_ms, result.segments[0].end_ms) == (2000, 5000)
        assert any("swapped" in w for w in result.warnings)

    def test_keeps_timings_of_a_text_less_block_and_warns(self):
        # Losing the block would shift nothing, but the silent window is real
        # timeline content -- keep it and say so.
        content = (
            "1\n00:00:00,000 --> 00:00:02,000\n\n\n"
            "2\n00:00:02,000 --> 00:00:04,000\nHi.\n"
        )
        result = parse_srt(content)
        assert len(result.segments) == 2
        assert result.segments[0].text == ""
        assert result.segments[0].source_text == ""
        assert any("no text" in w for w in result.warnings)

    def test_extra_blank_lines_between_blocks(self):
        content = (
            "1\n00:00:00,000 --> 00:00:02,000\nOne.\n\n\n\n"
            "2\n00:00:02,000 --> 00:00:04,000\nTwo.\n"
        )
        assert len(parse_srt(content).segments) == 2

    def test_rejects_empty_file(self):
        with pytest.raises(FileFormatError):
            parse_srt("   \n\n  ")

    def test_rejects_file_with_no_valid_blocks(self):
        with pytest.raises(FileFormatError):
            parse_srt("just some prose\nwith no timings at all")


class TestSourceTextCapture:
    def test_source_text_matches_on_import(self):
        segments = parse_srt(FIXTURE.read_text()).segments
        assert all(s.source_text == s.text for s in segments)
        assert not any(s.is_edited for s in segments)


class TestPlainText:
    def test_splits_into_sentences(self):
        result = parse_plain_text("Hello there. This is PediAid. Welcome aboard.")
        assert len(result.segments) == 3

    def test_segments_do_not_overlap(self):
        result = parse_plain_text("One sentence here. Another sentence here.")
        for previous, following in zip(result.segments, result.segments[1:]):
            assert following.start_ms >= previous.end_ms

    def test_warns_that_timings_are_estimated(self):
        result = parse_plain_text("Hello there.")
        assert any("estimated" in w for w in result.warnings)

    def test_keeps_abbreviations_together(self):
        result = parse_plain_text("Ask Dr. Rao about the dose. Then proceed.")
        assert len(result.segments) == 2

    def test_strips_markdown(self):
        result = parse_plain_text("# Heading\n\nSome **bold** text here.")
        assert "**" not in result.segments[-1].text
        assert "#" not in result.segments[0].text

    def test_rejects_empty(self):
        with pytest.raises(FileFormatError):
            parse_plain_text("   ")


class TestLoad:
    def test_loads_srt_by_extension(self, tmp_path):
        path = tmp_path / "x.srt"
        path.write_text(FIXTURE.read_text())
        assert load(path).count == 3

    def test_loads_txt_by_extension(self, tmp_path):
        path = tmp_path / "x.txt"
        path.write_text("Hello there. Second sentence.")
        assert load(path).source_format == "txt"

    def test_rejects_unknown_extension(self, tmp_path):
        path = tmp_path / "x.mp4"
        path.write_bytes(b"\x00\x01")
        with pytest.raises(UnsupportedFileError):
            load(path)
