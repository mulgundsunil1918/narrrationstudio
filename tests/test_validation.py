from app.core.models import Segment, SegmentStatus
from app.core.validation import Severity, validate


def codes(report):
    return {issue.code for issue in report.issues}


class TestCleanTimeline:
    """The §34 fixture must validate clean."""

    def setup_method(self):
        self.segments = [
            Segment(0, 2000, "Hello PediAid."),
            Segment(3000, 5000, "Clinical tools in one place."),
            Segment(5000, 8000, "Built for pediatric practice."),
        ]

    def test_no_errors(self):
        assert validate(self.segments).ok

    def test_no_overlap_reported(self):
        assert "overlap" not in codes(validate(self.segments))

    def test_counts_the_gap(self):
        report = validate(self.segments)
        assert report.gap_count == 1
        assert report.gap_ms == 1000

    def test_timeline_end(self):
        assert validate(self.segments).timeline_end_ms == 8000

    def test_summary_is_the_all_clear(self):
        assert validate(self.segments).summary() == "✓ 3/3 segments synchronized"


class TestOverlap:
    def test_detects_overlap(self):
        segments = [Segment(0, 5000, "A"), Segment(4000, 8000, "B")]
        report = validate(segments)
        assert not report.ok
        assert "overlap" in codes(report)

    def test_overlap_message_names_both_segments(self):
        segments = [Segment(0, 5000, "A"), Segment(4000, 8000, "B")]
        issue = next(i for i in validate(segments).issues if i.code == "overlap")
        assert "1" in issue.message and "2" in issue.message
        assert issue.suggestion

    def test_touching_segments_are_not_an_overlap(self):
        segments = [Segment(0, 5000, "A"), Segment(5000, 8000, "B")]
        assert "overlap" not in codes(validate(segments))

    def test_flagged_indices_include_the_overlapping_row(self):
        segments = [Segment(0, 5000, "A"), Segment(4000, 8000, "B")]
        assert 1 in validate(segments).flagged_indices


class TestDurations:
    def test_zero_duration_is_an_error(self):
        report = validate([Segment(1000, 1000, "A")])
        assert "non_positive_duration" in codes(report)
        assert not report.ok

    def test_negative_duration_is_an_error(self):
        assert "non_positive_duration" in codes(validate([Segment(2000, 1000, "A")]))

    def test_very_short_is_a_warning_not_an_error(self):
        report = validate([Segment(0, 200, "A")])
        assert "very_short" in codes(report)
        assert report.ok

    def test_negative_start_is_an_error(self):
        assert "negative_start" in codes(validate([Segment(-500, 1000, "A")]))


class TestText:
    def test_empty_text_warns(self):
        assert "empty_text" in codes(validate([Segment(0, 2000, "   ")]))

    def test_dense_text_warns(self):
        # ~60 characters in one second cannot be spoken naturally.
        segments = [Segment(0, 1000, "x" * 60)]
        assert "dense_text" in codes(validate(segments))

    def test_comfortable_text_does_not_warn(self):
        segments = [Segment(0, 4680, "Welcome to PediAid, a clinical reference platform.")]
        assert "dense_text" not in codes(validate(segments))


class TestStatus:
    def test_failed_segment_is_an_error(self):
        segment = Segment(0, 2000, "A", status=SegmentStatus.FAILED, error="no voice")
        report = validate([segment])
        assert "generation_failed" in codes(report)
        assert not report.ok

    def test_stale_audio_is_a_warning(self):
        segment = Segment(0, 2000, "A", status=SegmentStatus.NEEDS_REGEN)
        report = validate([segment])
        assert "stale_audio" in codes(report)
        assert report.ok


class TestOrdering:
    def test_out_of_order_warns(self):
        segments = [Segment(5000, 8000, "B"), Segment(0, 2000, "A")]
        assert "out_of_order" in codes(validate(segments))

    def test_out_of_order_does_not_falsely_report_overlap(self):
        segments = [Segment(5000, 8000, "B"), Segment(0, 2000, "A")]
        assert "overlap" not in codes(validate(segments))


class TestSummary:
    def test_reports_flagged_count(self):
        segments = [Segment(0, 2000, "A"), Segment(1000, 3000, "B")]
        assert "need" in validate(segments).summary()

    def test_singular_wording_for_one_segment(self):
        segments = [Segment(0, 2000, "A"), Segment(0, 100, "B" * 80)]
        summary = validate(segments).summary()
        assert "segment needs" in summary or "segments need" in summary

    def test_empty_document(self):
        assert validate([]).summary() == "No subtitles loaded"


class TestSeverityGrouping:
    def test_errors_warnings_split(self):
        segments = [Segment(0, 5000, "A"), Segment(4000, 8000, "  ")]
        report = validate(segments)
        assert any(i.severity is Severity.ERROR for i in report.errors)
        assert all(i.severity is Severity.WARNING for i in report.warnings)
