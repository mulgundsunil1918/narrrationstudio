"""Narration: how continuous the speech should be.

The technical vocabulary stays out of the way. "Narration segment" is what the
user sees; "narration group" lives in the engine. The maximum-segment control
carries an explicit note that it is not a limit on project length, because that
is the single most likely misreading.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QHBoxLayout,
    QRadioButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.core.timecode import format_display
from app.narration.groups import NarrationMode
from app.narration.report import preview_plan
from app.ui.state import AppState
from app.ui.theme import palette
from app.ui.widgets.common import (
    Card,
    Divider,
    LabeledSlider,
    Metric,
    Pill,
    SecondaryButton,
    Segmented,
    caption,
    clear_layout,
    clock,
    heading,
    label,
    muted,
    section_label,
    title,
)
from app.ui.widgets.waveform import TimelineView

SEGMENT_CHOICES = [(20, "20 sec"), (60, "60 sec"), (120, "120 sec"), (600, "No limit")]


class ModeOption(Card):
    """A radio choice with a plain-language description."""

    chosen = Signal(object)

    def __init__(self, mode: NarrationMode, recommended: bool, group: QButtonGroup, parent=None):
        super().__init__(parent=parent)
        self._mode = mode
        self.body.setContentsMargins(16, 14, 16, 15)
        self.body.setSpacing(6)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        top = QHBoxLayout()
        top.setSpacing(9)
        self.radio = QRadioButton(mode.label)
        font = self.radio.font()
        font.setPointSize(13)
        font.setWeight(font.Weight.DemiBold)
        self.radio.setFont(font)
        self.radio.toggled.connect(self._on_toggled)
        group.addButton(self.radio)
        top.addWidget(self.radio)
        if recommended:
            top.addWidget(Pill("Recommended", "info"))
        top.addStretch(1)
        self.add_layout(top)

        self.add(muted(mode.description, wrap=True))

    def _on_toggled(self, checked: bool) -> None:
        self._restyle(checked)
        if checked:
            self.chosen.emit(self._mode)

    def _restyle(self, active: bool) -> None:
        current = palette()
        self.setStyleSheet(
            f"#Card {{ background-color: {current.accent_soft if active else current.surface};"
            f" border: {'2px' if active else '1px'} solid"
            f" {current.accent if active else current.border}; border-radius: 12px; }}"
        )

    def mousePressEvent(self, event) -> None:
        self.radio.setChecked(True)
        super().mousePressEvent(event)


class NarrationScreen(QWidget):
    """Narration mode, segment length, and the timeline preview."""

    def __init__(self, state: AppState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._state = state

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build_header())

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        holder = QWidget()
        holder.setObjectName("Workspace")
        self._column = QVBoxLayout(holder)
        self._column.setContentsMargins(28, 22, 28, 32)
        self._column.setSpacing(18)
        self._column.setAlignment(Qt.AlignmentFlag.AlignTop)

        self._column.addWidget(self._build_modes())
        self._column.addWidget(self._build_length())
        self._column.addWidget(self._build_explainer())
        self._column.addWidget(self._build_timeline())
        self._column.addWidget(self._build_segments())
        self._scroll.setWidget(holder)
        outer.addWidget(self._scroll, 1)

        state.project_changed.connect(self.refresh)
        state.narration_changed.connect(self.refresh)
        state.advanced_changed.connect(lambda _a: self.refresh())
        self.refresh()

    # -- construction ----------------------------------------------------

    def _build_header(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("TopBar")
        row = QHBoxLayout(bar)
        row.setContentsMargins(28, 16, 28, 16)
        column = QVBoxLayout()
        column.setSpacing(2)
        column.addWidget(title("Narration"))
        column.addWidget(muted("Control how continuous the speech sounds."))
        row.addLayout(column)
        row.addStretch(1)
        self._summary_pill = Pill("—", "neutral")
        row.addWidget(self._summary_pill)
        return bar

    def _build_modes(self) -> QWidget:
        wrapper = QWidget()
        column = QVBoxLayout(wrapper)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(10)
        column.addWidget(section_label("Narration mode"))

        self._group = QButtonGroup(self)
        self._options: dict[NarrationMode, ModeOption] = {}
        for mode in (NarrationMode.NATURAL, NarrationMode.EXACT, NarrationMode.MANUAL):
            option = ModeOption(mode, mode is NarrationMode.NATURAL, self._group)
            option.chosen.connect(self._on_mode)
            column.addWidget(option)
            self._options[mode] = option
        self._options[self._state.narration.mode].radio.setChecked(True)
        return wrapper

    def _build_length(self) -> QWidget:
        card = Card()
        card.add(section_label("Maximum narration segment"))

        current = str(self._state.narration.max_group_ms // 1000)
        if current not in {str(v) for v, _ in SEGMENT_CHOICES}:
            current = "60"
        self._length = Segmented(
            [(str(v), text) for v, text in SEGMENT_CHOICES], initial=current
        )
        self._length.changed.connect(self._on_length)
        card.add(self._length)

        note = muted(
            "This controls internal processing only. It does NOT limit the length "
            "of your video or project — a 20-minute script produces 20 minutes of "
            "narration.",
            wrap=True,
        )
        card.add(note)

        if self._state.advanced:
            card.add(Divider())
            card.add(section_label("Advanced"))
            self._crossfade = LabeledSlider(
                "Micro-crossfade between segments", 0, 80, self._state.narration.crossfade_ms, " ms"
            )
            self._crossfade.valueChanged.connect(self._on_crossfade)
            card.add(self._crossfade)
            card.add(
                caption(
                    "Smooths the join between two separately generated segments. "
                    "Never overlaps words and never changes the timeline.",
                    wrap=True,
                )
            )
        return card

    def _build_explainer(self) -> QWidget:
        """Shown only when the length cap had to cut mid-sentence."""
        self._explainer = Card()
        self._explainer.setVisible(False)
        self._explainer.add(section_label("About “continues the previous sentence”"))
        self._explainer_body = muted("", wrap=True)
        self._explainer.add(self._explainer_body)
        self._explainer.add(
            caption(
                "This is a note, not a problem. The narration still generates and "
                "stays exactly on your subtitle timings — you will just hear a "
                "small pause where the segment starts.",
                wrap=True,
            )
        )
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addStretch(1)
        self._fix_120 = SecondaryButton("Use 120 second segments")
        self._fix_120.clicked.connect(lambda: self._length.select("120"))
        row.addWidget(self._fix_120)
        self._fix_none = SecondaryButton("Remove the limit")
        self._fix_none.clicked.connect(lambda: self._length.select("600"))
        row.addWidget(self._fix_none)
        self._explainer.add_layout(row)
        return self._explainer

    def _build_timeline(self) -> QWidget:
        card = Card()
        card.add(section_label("Timeline"))
        card.add(
            muted(
                "Captions change on their own timestamps while narration runs "
                "continuously underneath. Amber marks a segment that had to start "
                "mid-sentence.",
                wrap=True,
            )
        )
        self.timeline = TimelineView()
        card.add(self.timeline)
        return card

    def _build_segments(self) -> QWidget:
        self._segments_card = Card()
        self._segments_card.add(section_label("Narration segments"))
        self._segments_holder = QVBoxLayout()
        self._segments_holder.setSpacing(8)
        self._segments_card.add_layout(self._segments_holder)
        return self._segments_card

    # -- state -----------------------------------------------------------

    def _on_mode(self, mode: NarrationMode) -> None:
        if mode is self._state.narration.mode:
            return
        self._state.narration.mode = mode
        self._state.invalidate_plan()
        self._state.report(f"Narration mode: {mode.label}", "info")

    def _on_length(self, key: str) -> None:
        seconds = int(key)
        milliseconds = seconds * 1000
        if milliseconds == self._state.narration.max_group_ms:
            return
        self._state.narration.max_group_ms = milliseconds
        self._state.invalidate_plan()

    def _on_crossfade(self, value: int) -> None:
        self._state.narration.crossfade_ms = value

    def refresh(self) -> None:
        segments = self._state.segments
        clear_layout(self._segments_holder)

        if not segments:
            self._summary_pill.set_status("No script loaded", "neutral")
            self.timeline.set_data([], [], 1)
            self._segments_holder.addWidget(
                muted("Import a script to see how it will be narrated.", wrap=True)
            )
            return

        plan = self._state.plan()
        window = self._state.window()

        captions = [(s.start_ms, s.end_ms) for s in segments]
        blocks = [
            (window.start_ms(group), window.end_ms(group), group.forced_cut)
            for group in plan
        ]
        self.timeline.set_data(captions, blocks, self._state.timeline_ms)

        forced = sum(1 for group in plan if group.forced_cut)
        if forced:
            self._summary_pill.set_status(
                f"{len(plan)} segments · {forced} continue a sentence", "warning"
            )
        else:
            self._summary_pill.set_status(f"{len(plan)} narration segments", "success")
        self._update_explainer(forced, segments)

        rows = preview_plan(plan, segments)
        for row in rows[:60]:
            self._segments_holder.addWidget(self._segment_row(row))

    def _update_explainer(self, forced: int, segments) -> None:
        if not forced:
            self._explainer.setVisible(False)
            return

        # Count how many captions actually end a sentence. When that number is
        # tiny the cause is the subtitle file, not a setting, and saying so is
        # more useful than repeating the warning.
        from app.narration.grouping import _ends_sentence

        natural = sum(1 for s in segments if _ends_sentence(s.text.strip()))
        current = self._state.narration.max_group_ms // 1000
        self._explainer_body.setText(
            f"{forced} of these segments begin in the middle of a sentence. Only "
            f"{natural} of your {len(segments)} subtitles end on a full stop, so "
            "there are very few natural pauses to break at, and the "
            f"{current}-second limit has to cut somewhere.\n\n"
            "Longer segments mean fewer cuts, but the narration drifts a little "
            "further from the caption on screen. Adding full stops in the Script "
            "step removes them properly."
        )
        self._fix_120.setVisible(current < 120)
        self._fix_none.setVisible(current < 600)
        self._explainer.setVisible(True)

    def _segment_row(self, row) -> QWidget:
        card = Card(quiet=True)
        card.body.setContentsMargins(14, 11, 14, 12)
        card.body.setSpacing(5)

        header = QHBoxLayout()
        header.setSpacing(9)
        header.addWidget(label(f"Narration Segment {row.number}", "Heading"))
        header.addStretch(1)
        header.addWidget(
            muted(f"{clock(row.start_ms)} → {clock(row.end_ms)}  ·  {row.duration_s:.1f}s")
        )
        card.add_layout(header)

        if self._state.advanced:
            card.add(
                caption(
                    f"Captions {row.caption_span[0]}–{row.caption_span[1]}"
                    + ("  ·  forced cut" if row.forced_cut else "")
                )
            )

        text = row.text if len(row.text) < 190 else row.text[:187] + "…"
        card.add(muted(text, wrap=True))

        if row.forced_cut:
            badge = Pill("Continues the previous sentence", "warning")
            badge.setAlignment(Qt.AlignmentFlag.AlignLeft)
            card.add(badge)
        if row.gap_before_ms > 0:
            card.add(
                caption(
                    f"{row.gap_before_ms / 1000:.1f}s pause before this segment, "
                    "from your subtitle timings"
                )
            )
        return card
