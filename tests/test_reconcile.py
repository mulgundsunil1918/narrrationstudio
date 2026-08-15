"""Bringing an AI-edited script back: the words come in, the clock does not.

An AI told to leave timestamps alone mostly does. These tests are about the
times it does not — renumbering, merging, rounding, dropping the tail, helpfully
retiming a line to suit new wording. In every one of those cases the open
script's timings must survive untouched, and the user must be told what did not
line up.
"""

from __future__ import annotations

from app.core.models import Segment
from app.srt.reconcile import (
    from_plain_text,
    reconcile,
    to_plain_text,
)

SCRIPT = [
    Segment(0, 2000, "Welcome to the demo"),
    Segment(2000, 4500, "this is the second line"),
    Segment(4500, 7000, "and here is the third"),
]


def copy_of(segments, overrides: dict[int, str] | None = None):
    """The same script back, with the given lines reworded."""
    overrides = overrides or {}
    return [
        Segment(s.start_ms, s.end_ms, overrides.get(i, s.text))
        for i, s in enumerate(segments)
    ]


# -- the happy path ------------------------------------------------------


def test_polished_wording_is_taken():
    returned = copy_of(SCRIPT, {0: "Welcome to the demo.", 1: "This is the second line,"})
    result = reconcile(SCRIPT, returned)

    assert result.matched == 3
    assert result.is_clean
    assert result.changed == 2
    assert result.as_text_map() == {
        0: "Welcome to the demo.",
        1: "This is the second line,",
    }


def test_identical_file_changes_nothing():
    result = reconcile(SCRIPT, list(SCRIPT))
    assert result.matched == 3
    assert result.changes == []
    assert result.as_text_map() == {}


def test_whitespace_only_difference_is_not_a_change():
    returned = [
        Segment(s.start_ms, s.end_ms, f"  {s.text}  ") for s in SCRIPT
    ]
    result = reconcile(SCRIPT, returned)
    assert result.changes == []


# -- the AI misbehaving --------------------------------------------------


def test_retimed_subtitles_keep_the_original_timings():
    """The whole point: new words, old clock."""
    returned = [
        Segment(0, 2500, "Welcome to the demo."),        # end moved
        Segment(2000, 4500, "This is the second line,"),
        Segment(4500, 7000, "and here is the third."),
    ]
    result = reconcile(SCRIPT, returned)

    assert result.retimed == [0]
    assert not result.is_clean
    # Only text is ever offered up; there is no timing in the payload at all.
    assert set(result.as_text_map()) == {0, 1, 2}
    assert any("timings" in problem for problem in result.problems)


def test_wholesale_retiming_falls_back_to_position():
    """Some models rewrite every timestamp. Order is then all there is."""
    returned = [
        Segment(0, 1900, "Welcome to the demo."),
        Segment(1900, 4000, "This is the second line,"),
        Segment(4000, 6800, "and here is the third."),
    ]
    result = reconcile(SCRIPT, returned)

    assert result.strategy == "position"
    assert result.matched == 3
    assert result.changed == 3
    assert len(result.retimed) == 3
    assert any("timings" in problem for problem in result.problems)


def test_merged_subtitles_leave_the_rest_alone():
    """Two captions merged into one: the orphan keeps its current wording."""
    returned = [
        Segment(0, 4500, "Welcome to the demo. This is the second line,"),
        Segment(4500, 7000, "and here is the third."),
    ]
    result = reconcile(SCRIPT, returned)

    assert 1 in result.missing
    assert 1 not in result.as_text_map()
    assert any("not in the returned file" in problem for problem in result.problems)


def test_truncated_file_reports_what_is_missing():
    """A model that stops early must not silently delete the tail."""
    returned = copy_of(SCRIPT)[:1]
    result = reconcile(SCRIPT, returned)

    assert result.matched == 1
    assert result.missing == [1, 2]
    assert any("2 subtitle" in problem for problem in result.problems)


def test_invented_subtitles_are_ignored():
    returned = copy_of(SCRIPT) + [Segment(9000, 11000, "A line I never said.")]
    result = reconcile(SCRIPT, returned)

    assert result.extra == 1
    assert result.matched == 3
    assert len(result.as_text_map()) == 0   # nothing was reworded
    assert any("do not match" in problem for problem in result.problems)


def test_millisecond_rounding_still_matches():
    returned = [
        Segment(s.start_ms + 12, s.end_ms, s.text.upper()) for s in SCRIPT
    ]
    result = reconcile(SCRIPT, returned)
    assert result.matched == 3
    assert result.strategy == "time"


def test_a_file_from_a_different_video_is_refused():
    other = [Segment(600_000, 602_000, "Completely unrelated.")]
    result = reconcile(SCRIPT, other)

    assert not result.is_usable
    assert result.as_text_map() == {}
    assert any("different video" in problem for problem in result.problems)


def test_empty_returned_file_is_refused():
    result = reconcile(SCRIPT, [])
    assert not result.is_usable
    assert result.problems


def test_no_open_script_is_refused():
    result = reconcile([], copy_of(SCRIPT))
    assert not result.is_usable
    assert result.problems


def test_blank_replacement_text_never_wipes_a_caption():
    returned = [
        Segment(0, 2000, "   "),
        Segment(2000, 4500, "this is the second line"),
        Segment(4500, 7000, "and here is the third"),
    ]
    result = reconcile(SCRIPT, returned)
    assert 0 not in result.as_text_map()


# -- the plain-text route ------------------------------------------------


def test_plain_text_round_trip():
    text = to_plain_text(SCRIPT)
    assert text.splitlines() == [s.text for s in SCRIPT]

    edited = "Welcome to the demo.\nThis is the second line,\nand here is the third."
    result = from_plain_text(SCRIPT, edited)
    assert result.matched == 3
    assert result.changed == 3


def test_plain_text_with_a_different_line_count_is_refused():
    """Silently shifting every line by one would be the worst outcome here."""
    result = from_plain_text(SCRIPT, "Only one line came back.")
    assert not result.is_usable
    assert result.as_text_map() == {}
    assert any(".srt" in problem for problem in result.problems)


def test_plain_text_ignores_blank_lines_between_paragraphs():
    edited = "One.\n\nTwo.\n\n\nThree.\n"
    result = from_plain_text(SCRIPT, edited)
    assert result.matched == 3
    assert result.as_text_map() == {0: "One.", 1: "Two.", 2: "Three."}


def test_empty_plain_text_is_refused():
    result = from_plain_text(SCRIPT, "   \n\n ")
    assert not result.is_usable


# -- applying it ---------------------------------------------------------


def test_applying_changes_leaves_every_timing_untouched():
    from app.core.document import SubtitleDocument

    document = SubtitleDocument()
    document.load([Segment(s.start_ms, s.end_ms, s.text) for s in SCRIPT])
    before = [(s.start_ms, s.end_ms) for s in document.segments]

    returned = [
        Segment(0, 9999, "Welcome to the demo."),
        Segment(2000, 4500, "This is the second line,"),
        Segment(4500, 7000, "and here is the third."),
    ]
    result = reconcile(document.segments, returned)
    document.apply_text_map(result.as_text_map(), "Polish script")

    assert [(s.start_ms, s.end_ms) for s in document.segments] == before
    assert document.segments[0].text == "Welcome to the demo."


def test_applying_is_undoable():
    from app.core.document import SubtitleDocument

    document = SubtitleDocument()
    document.load([Segment(s.start_ms, s.end_ms, s.text) for s in SCRIPT])

    result = reconcile(document.segments, copy_of(SCRIPT, {0: "Reworded."}))
    document.apply_text_map(result.as_text_map(), "Polish script")
    assert document.segments[0].text == "Reworded."

    document.undo()
    assert document.segments[0].text == "Welcome to the demo"
