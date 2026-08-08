from pathlib import Path

from app.core.models import Segment
from app.srt.parser import parse_srt
from app.srt.writer import to_srt, write_srt

FIXTURE = Path(__file__).parent / "fixtures" / "sample.srt"


class TestRoundTrip:
    def test_import_export_preserves_timings_exactly(self):
        original = parse_srt(FIXTURE.read_text()).segments
        reparsed = parse_srt(to_srt(original)).segments
        assert [(s.start_ms, s.end_ms) for s in reparsed] == [
            (s.start_ms, s.end_ms) for s in original
        ]

    def test_import_export_preserves_text(self):
        original = parse_srt(FIXTURE.read_text()).segments
        reparsed = parse_srt(to_srt(original)).segments
        assert [s.text for s in reparsed] == [s.text for s in original]

    def test_output_matches_the_source_file_byte_for_byte(self):
        original = parse_srt(FIXTURE.read_text()).segments
        assert to_srt(original) == FIXTURE.read_text()


class TestFormatting:
    def test_numbers_sequentially_from_one(self):
        segments = [Segment(0, 1000, "A"), Segment(1000, 2000, "B")]
        assert to_srt(segments).startswith("1\n")
        assert "\n2\n" in to_srt(segments)

    def test_uses_comma_separator(self):
        assert "00:00:00,000 --> 00:00:01,000" in to_srt([Segment(0, 1000, "A")])

    def test_orders_by_start_time(self):
        segments = [Segment(5000, 6000, "Later"), Segment(0, 1000, "Earlier")]
        output = to_srt(segments)
        assert output.index("Earlier") < output.index("Later")

    def test_empty_text_becomes_a_space_so_the_block_stays_valid(self):
        output = to_srt([Segment(0, 1000, "")])
        assert len(parse_srt(output).segments) == 1

    def test_ends_with_a_newline(self):
        assert to_srt([Segment(0, 1000, "A")]).endswith("\n")


class TestWriteFile:
    def test_writes_and_reads_back(self, tmp_path):
        path = tmp_path / "out.srt"
        segments = parse_srt(FIXTURE.read_text()).segments
        write_srt(path, segments)
        assert len(parse_srt(path.read_text()).segments) == 3

    def test_leaves_no_temporary_file(self, tmp_path):
        path = tmp_path / "out.srt"
        write_srt(path, [Segment(0, 1000, "A")])
        assert list(tmp_path.iterdir()) == [path]

    def test_overwrites_existing(self, tmp_path):
        path = tmp_path / "out.srt"
        write_srt(path, [Segment(0, 1000, "First")])
        write_srt(path, [Segment(0, 1000, "Second")])
        assert "Second" in path.read_text()
        assert "First" not in path.read_text()
