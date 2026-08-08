import pytest

from app.core.document import DocumentError, SubtitleDocument
from app.core.models import Segment, SegmentStatus


def make_document():
    return SubtitleDocument(
        [
            Segment(0, 2000, "Hello PediAid."),
            Segment(3000, 5000, "Clinical tools in one place."),
            Segment(5000, 8000, "Built for pediatric practice."),
        ]
    )


class TestBasics:
    def test_length_and_access(self):
        document = make_document()
        assert len(document) == 3
        assert document.at(0).text == "Hello PediAid."

    def test_timeline_end_is_last_end(self):
        assert make_document().timeline_end_ms == 8000

    def test_total_speech_excludes_gaps(self):
        assert make_document().total_speech_ms == 7000

    def test_out_of_range_access_is_friendly(self):
        with pytest.raises(DocumentError):
            make_document().at(99)

    def test_lookup_by_uid(self):
        document = make_document()
        uid = document.at(1).uid
        assert document.index_of(uid) == 1
        assert document.by_uid(uid).text == "Clinical tools in one place."


class TestTextEditing:
    def test_set_text_changes_it(self):
        document = make_document()
        document.set_text(0, "Hello there.")
        assert document.at(0).text == "Hello there."

    def test_editing_generated_text_marks_needs_regen(self):
        document = make_document()
        document.set_status(0, SegmentStatus.GENERATED)
        document.set_text(0, "Different words.")
        assert document.at(0).status is SegmentStatus.NEEDS_REGEN

    def test_editing_does_not_regenerate_automatically(self):
        # §3: never generate as a side effect of an edit.
        document = make_document()
        document.set_status(0, SegmentStatus.GENERATED)
        document.set_text(0, "Different words.")
        assert document.at(0).status is not SegmentStatus.GENERATING

    def test_ungenerated_segment_stays_pending(self):
        document = make_document()
        document.set_text(0, "Different words.")
        assert document.at(0).status is SegmentStatus.PENDING

    def test_identical_text_is_a_no_op(self):
        document = make_document()
        assert document.set_text(0, "Hello PediAid.") == 0
        assert not document.can_undo

    def test_edited_flag_and_revert(self):
        document = make_document()
        document.set_text(0, "Changed.")
        assert document.at(0).is_edited
        document.revert_text([0])
        assert document.at(0).text == "Hello PediAid."
        assert not document.at(0).is_edited

    def test_apply_text_map(self):
        document = make_document()
        changed = document.apply_text_map({0: "A.", 2: "C."}, "Cleanup")
        assert changed == 2
        assert [s.text for s in document] == ["A.", "Clinical tools in one place.", "C."]


class TestTimingEdits:
    def test_set_times_updates_window(self):
        document = make_document()
        document.set_times(0, start_ms=100, end_ms=2500)
        assert (document.at(0).start_ms, document.at(0).end_ms) == (100, 2500)

    def test_rejects_negative_start(self):
        with pytest.raises(DocumentError):
            make_document().set_times(0, start_ms=-1)

    def test_rejects_inverted_window(self):
        with pytest.raises(DocumentError):
            make_document().set_times(0, start_ms=2000, end_ms=1000)

    def test_rejects_too_short_window(self):
        with pytest.raises(DocumentError):
            make_document().set_times(0, start_ms=0, end_ms=50)

    def test_changing_duration_invalidates_generated_audio(self):
        document = make_document()
        document.set_status(0, SegmentStatus.GENERATED)
        document.set_times(0, end_ms=3000)
        assert document.at(0).status is SegmentStatus.NEEDS_REGEN

    def test_moving_without_changing_duration_keeps_audio(self):
        # The fit still holds: the window is the same length, just later.
        document = make_document()
        document.set_status(0, SegmentStatus.GENERATED)
        document.set_times(0, start_ms=500, end_ms=2500)
        assert document.at(0).status is SegmentStatus.GENERATED


class TestSplit:
    def test_produces_two_segments(self):
        document = make_document()
        document.split(1)
        assert len(document) == 4

    def test_split_preserves_the_original_window_exactly(self):
        document = make_document()
        start, end = document.at(1).start_ms, document.at(1).end_ms
        document.split(1)
        assert document.at(1).start_ms == start
        assert document.at(2).end_ms == end
        assert document.at(1).end_ms == document.at(2).start_ms

    def test_split_never_changes_timeline_length(self):
        document = make_document()
        before = document.timeline_end_ms
        document.split(2)
        assert document.timeline_end_ms == before

    def test_split_divides_the_text(self):
        document = make_document()
        document.split(1)
        combined = f"{document.at(1).text} {document.at(2).text}"
        assert combined == "Clinical tools in one place."

    def test_refuses_split_with_empty_side(self):
        document = make_document()
        with pytest.raises(DocumentError):
            document.split(0, char_offset=0)

    def test_refuses_split_of_a_very_short_segment(self):
        document = SubtitleDocument([Segment(0, 150, "Hi there now")])
        with pytest.raises(DocumentError):
            document.split(0)


class TestMerge:
    def test_merges_consecutive(self):
        document = make_document()
        document.merge([1, 2])
        assert len(document) == 2

    def test_merged_window_spans_both(self):
        document = make_document()
        document.merge([1, 2])
        assert (document.at(1).start_ms, document.at(1).end_ms) == (3000, 8000)

    def test_merged_text_is_joined(self):
        document = make_document()
        document.merge([1, 2])
        assert document.at(1).text == (
            "Clinical tools in one place. Built for pediatric practice."
        )

    def test_refuses_non_consecutive(self):
        with pytest.raises(DocumentError):
            make_document().merge([0, 2])

    def test_refuses_single_selection(self):
        with pytest.raises(DocumentError):
            make_document().merge([0])


class TestDuplicateDeleteInsert:
    def test_duplicate_adds_a_row(self):
        document = make_document()
        document.duplicate(0)
        assert len(document) == 4
        assert document.at(1).text == "Hello PediAid."

    def test_duplicate_does_not_overlap(self):
        document = make_document()
        document.duplicate(0)
        assert document.at(0).end_ms == document.at(1).start_ms
        assert document.at(1).end_ms <= document.at(2).start_ms

    def test_delete_removes_rows(self):
        document = make_document()
        document.delete([0, 2])
        assert len(document) == 1
        assert document.at(0).text == "Clinical tools in one place."

    def test_delete_leaves_the_gap_as_silence(self):
        document = make_document()
        document.delete([0])
        assert document.at(0).start_ms == 3000  # nothing shifted left

    def test_insert_uses_the_existing_gap(self):
        document = make_document()
        document.insert_after(0, "Inserted.")
        assert (document.at(1).start_ms, document.at(1).end_ms) == (2000, 3000)

    def test_insert_refuses_when_there_is_no_gap(self):
        document = make_document()
        with pytest.raises(DocumentError):
            document.insert_after(1)  # 1 and 2 are contiguous


class TestUndoRedo:
    def test_undo_restores_text(self):
        document = make_document()
        document.set_text(0, "Changed.")
        document.undo()
        assert document.at(0).text == "Hello PediAid."

    def test_redo_reapplies(self):
        document = make_document()
        document.set_text(0, "Changed.")
        document.undo()
        document.redo()
        assert document.at(0).text == "Changed."

    def test_undo_restores_structure(self):
        document = make_document()
        document.merge([0, 1])
        document.undo()
        assert len(document) == 3
        assert document.at(0).text == "Hello PediAid."

    def test_undo_stack_is_multi_level(self):
        document = make_document()
        document.set_text(0, "One.")
        document.set_text(0, "Two.")
        document.set_text(0, "Three.")
        document.undo()
        document.undo()
        assert document.at(0).text == "One."

    def test_new_edit_clears_redo(self):
        document = make_document()
        document.set_text(0, "One.")
        document.undo()
        document.set_text(0, "Other.")
        assert not document.can_redo

    def test_labels_describe_the_action(self):
        document = make_document()
        document.merge([0, 1])
        assert document.undo_label == "Merge 2 subtitles"

    def test_undo_at_the_bottom_is_safe(self):
        document = make_document()
        document.undo()
        document.undo()
        assert len(document) == 3

    def test_load_clears_history(self):
        document = make_document()
        document.set_text(0, "Changed.")
        document.load([Segment(0, 1000, "New.")])
        assert not document.can_undo


class TestDirtyTracking:
    def test_starts_clean(self):
        assert not make_document().is_dirty

    def test_edit_marks_dirty(self):
        document = make_document()
        document.set_text(0, "Changed.")
        assert document.is_dirty

    def test_mark_saved_clears_it(self):
        document = make_document()
        document.set_text(0, "Changed.")
        document.mark_saved()
        assert not document.is_dirty


class TestListeners:
    def test_listener_fires_on_change(self):
        document = make_document()
        calls = []
        document.add_listener(lambda: calls.append(1))
        document.set_text(0, "Changed.")
        assert calls

    def test_listener_fires_on_undo(self):
        document = make_document()
        document.set_text(0, "Changed.")
        calls = []
        document.add_listener(lambda: calls.append(1))
        document.undo()
        assert calls


class TestSorting:
    def test_sorts_by_start_time(self):
        document = SubtitleDocument(
            [Segment(5000, 8000, "C"), Segment(0, 2000, "A"), Segment(3000, 5000, "B")]
        )
        document.sort_by_time()
        assert [s.text for s in document] == ["A", "B", "C"]

    def test_sorting_does_not_change_timestamps(self):
        document = SubtitleDocument([Segment(5000, 8000, "C"), Segment(0, 2000, "A")])
        document.sort_by_time()
        assert [(s.start_ms, s.end_ms) for s in document] == [(0, 2000), (5000, 8000)]

    def test_already_sorted_is_a_no_op(self):
        assert make_document().sort_by_time() == 0
