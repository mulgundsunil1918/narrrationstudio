"""Script review: the transcript editor.

Deliberately not a spreadsheet of SRT rows. Captions are shown as readable
paragraphs with a quiet timestamp in the margin, because the user is reading
a script, not engineering subtitles. Exact timings live in Advanced mode.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QFocusEvent, QKeyEvent
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLineEdit,
    QPlainTextEdit,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.core.document import DocumentError
from app.core.timecode import TimecodeError, format_display, parse_timestamp
from app.narration.groups import GroupWindow
from app.srt import cleanup
from app.ui.state import AppState
from app.ui.theme import BODY, mono_font, palette
from app.ui.widgets.common import (
    Card,
    GhostButton,
    HeightForWidthMixin,
    Pill,
    SecondaryButton,
    caption,
    clear_layout,
    clock,
    heading,
    label,
    muted,
    title,
)


class CaptionBlock(HeightForWidthMixin, QWidget):
    """One caption: timestamp in the margin, editable text beside it."""

    edited = Signal(int, str)
    focused = Signal(int)
    retimed = Signal(int, str, str)

    def __init__(self, index: int, state: AppState, advanced: bool, parent=None) -> None:
        super().__init__(parent)
        self._enable_height_for_width()
        self._index = index
        self._state = state
        self._advanced = advanced
        self._active = False

        segment = state.segments[index]

        row = QHBoxLayout(self)
        row.setContentsMargins(14, 11, 14, 11)
        row.setSpacing(16)

        # -- margin: time -------------------------------------------------
        margin = QVBoxLayout()
        margin.setSpacing(3)
        margin.setAlignment(Qt.AlignmentFlag.AlignTop)

        self._time = label(format_display(segment.start_ms)[:-4], "Muted")
        self._time.setFont(mono_font(11))
        self._time.setFixedWidth(78)
        margin.addWidget(self._time)

        self._number = caption(f"#{index + 1}")
        margin.addWidget(self._number)

        if advanced:
            self._start_edit = QLineEdit(format_display(segment.start_ms))
            self._end_edit = QLineEdit(format_display(segment.end_ms))
            for field in (self._start_edit, self._end_edit):
                field.setFont(mono_font(10))
                field.setInputMask("99:99:99.999;_")
                field.setFixedWidth(96)
                field.setStyleSheet("padding: 3px 5px; border-radius: 6px;")
            self._start_edit.editingFinished.connect(self._commit_times)
            self._end_edit.editingFinished.connect(self._commit_times)
            margin.addSpacing(4)
            margin.addWidget(self._start_edit)
            margin.addWidget(self._end_edit)
        else:
            self._start_edit = None
            self._end_edit = None

        row.addLayout(margin)

        # -- text ---------------------------------------------------------
        self._text = _GrowingTextEdit(segment.text)
        self._text.setObjectName("ScriptText")
        self._text.focus_gained.connect(lambda: self.focused.emit(self._index))
        self._text.editing_finished.connect(
            lambda: self.edited.emit(self._index, self._text.toPlainText())
        )
        row.addWidget(self._text, 1)

        self._apply_style()

    # -- appearance ------------------------------------------------------

    def set_active(self, active: bool) -> None:
        if active != self._active:
            self._active = active
            self._apply_style()

    def _apply_style(self) -> None:
        current = palette()
        if self._active:
            background = current.accent_soft
            border = current.accent
        else:
            background = "transparent"
            border = "transparent"
        self.setStyleSheet(
            f"CaptionBlock {{ background-color: {background};"
            f" border-left: 3px solid {border}; border-radius: 8px; }}"
        )
        self._text.setStyleSheet(
            f"QPlainTextEdit#ScriptText {{ background: transparent; border: none;"
            f" color: {current.text}; font-size: {BODY + 1}px; padding: 0px; }}"
            f"QPlainTextEdit#ScriptText:focus {{ background: {current.surface_alt};"
            f" border-radius: 7px; padding: 4px; }}"
        )

    def refresh(self) -> None:
        segment = self._state.segments[self._index]
        if self._text.toPlainText() != segment.text and not self._text.hasFocus():
            self._text.setPlainText(segment.text)
        self._time.setText(format_display(segment.start_ms)[:-4])

    def _commit_times(self) -> None:
        if self._start_edit and self._end_edit:
            self.retimed.emit(self._index, self._start_edit.text(), self._end_edit.text())


class _GrowingTextEdit(QPlainTextEdit):
    """A text field that sizes itself to its content."""

    focus_gained = Signal()
    editing_finished = Signal()

    def __init__(self, text: str, parent=None) -> None:
        super().__init__(text, parent)
        self.setFrameShape(QPlainTextEdit.Shape.NoFrame)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.document().contentsChanged.connect(self._resize_to_content)
        self._dirty = False
        self.textChanged.connect(lambda: setattr(self, "_dirty", True))
        self._resize_to_content()

    def _resize_to_content(self) -> None:
        """Match the widget height to the laid-out document.

        The document layout is the only thing that knows how many lines the text
        wraps to at the current width, so this has to run again on every resize —
        computing it once at build time gives every block the wrong height and
        makes them overlap.
        """
        self.document().setTextWidth(max(1, self.viewport().width()))
        height = self.document().documentLayout().documentSize().height()
        self.setFixedHeight(max(28, int(height) + 10))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._resize_to_content()

    def sizeHint(self) -> QSize:
        return QSize(400, self.height())

    def focusInEvent(self, event: QFocusEvent) -> None:
        super().focusInEvent(event)
        self.focus_gained.emit()

    def focusOutEvent(self, event: QFocusEvent) -> None:
        super().focusOutEvent(event)
        if self._dirty:
            self._dirty = False
            self.editing_finished.emit()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        # Enter commits; Shift+Enter inserts a line break.
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not (
            event.modifiers() & Qt.KeyboardModifier.ShiftModifier
        ):
            self.clearFocus()
            return
        super().keyPressEvent(event)


class ScriptScreen(QWidget):
    """The transcript editor plus its toolbar."""

    request_enhance = Signal()
    request_polish = Signal()
    continue_pressed = Signal()

    def __init__(self, state: AppState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._state = state
        self._blocks: list[CaptionBlock] = []
        self._active = -1

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build_header())
        outer.addWidget(self._build_scroller(), 1)

        state.project_changed.connect(self.rebuild)
        state.advanced_changed.connect(lambda _a: self.rebuild())

    # -- header ----------------------------------------------------------

    def _build_header(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("TopBar")
        row = QHBoxLayout(bar)
        row.setContentsMargins(28, 16, 28, 16)
        row.setSpacing(12)

        column = QVBoxLayout()
        column.setSpacing(2)
        column.addWidget(title("Script"))
        self._summary = muted("")
        column.addWidget(self._summary)
        row.addLayout(column)
        row.addStretch(1)

        self._search = QLineEdit()
        self._search.setPlaceholderText("Search the script…")
        self._search.setFixedWidth(220)
        self._search.textChanged.connect(self._filter)
        row.addWidget(self._search)

        self._undo = GhostButton("Undo")
        self._undo.clicked.connect(self._state.document.undo)
        self._redo = GhostButton("Redo")
        self._redo.clicked.connect(self._state.document.redo)
        row.addWidget(self._undo)
        row.addWidget(self._redo)

        enhance = GhostButton("Fix My Terms…")
        enhance.setToolTip(
            "Apply your own find-and-replace rules for names and jargon — "
            "instant, and stays on this Mac"
        )
        enhance.clicked.connect(self.request_enhance)
        row.addWidget(enhance)

        # The AI round trip is the one that fixes wording and punctuation, which
        # is what most transcripts actually need, so it leads.
        polish = SecondaryButton("Polish with AI…")
        polish.setToolTip(
            "Send the script to ChatGPT to be tidied, then bring it back — "
            "your timings are kept whatever it returns"
        )
        polish.clicked.connect(self.request_polish)
        row.addWidget(polish)
        return bar

    def _build_scroller(self) -> QWidget:
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        holder = QWidget()
        holder.setObjectName("Workspace")
        self._column = QVBoxLayout(holder)
        self._column.setContentsMargins(28, 22, 28, 40)
        self._column.setSpacing(2)
        self._column.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._scroll.setWidget(holder)
        return self._scroll

    # -- content ---------------------------------------------------------

    def rebuild(self) -> None:
        clear_layout(self._column)
        self._blocks.clear()

        segments = self._state.segments
        if not segments:
            from app.ui.widgets.common import EmptyState

            self._column.addWidget(
                EmptyState(
                    "📄",
                    "No script loaded",
                    "Import a subtitle file from Home to review and narrate it.",
                )
            )
            self._summary.setText("")
            return

        advanced = self._state.advanced
        for index in range(len(segments)):
            block = CaptionBlock(index, self._state, advanced)
            block.edited.connect(self._on_edited)
            block.focused.connect(self.set_active)
            block.retimed.connect(self._on_retimed)
            self._column.addWidget(block)
            self._blocks.append(block)

        self._refresh_summary()
        self._refresh_history()

    def _refresh_summary(self) -> None:
        count = len(self._state.segments)
        plan = self._state.plan()
        self._summary.setText(
            f"{count} subtitles · {clock(self._state.timeline_ms)} · "
            f"{len(plan)} narration segments"
        )

    def _refresh_history(self) -> None:
        self._undo.setEnabled(self._state.document.can_undo)
        self._redo.setEnabled(self._state.document.can_redo)

    # -- editing ---------------------------------------------------------

    def _on_edited(self, index: int, text: str) -> None:
        try:
            changed = self._state.document.set_text(index, text)
        except DocumentError as exc:
            self._state.report(str(exc), "error")
            return
        if changed:
            self._state.report("Script updated", "success")
        self._refresh_summary()
        self._refresh_history()

    def _on_retimed(self, index: int, start_text: str, end_text: str) -> None:
        try:
            start = parse_timestamp(start_text)
            end = parse_timestamp(end_text)
        except TimecodeError:
            self._state.report(
                "That timestamp is not valid. Use HH:MM:SS.mmm, for example 00:00:04.680.",
                "error",
            )
            self._blocks[index].refresh()
            return
        try:
            self._state.document.set_times(index, start_ms=start, end_ms=end)
        except DocumentError as exc:
            self._state.report(str(exc), "error")
            self._blocks[index].refresh()
            return
        self._refresh_summary()

    def _filter(self, needle: str) -> None:
        needle = needle.strip().lower()
        for index, block in enumerate(self._blocks):
            if not needle:
                block.setVisible(True)
                continue
            text = self._state.segments[index].text.lower()
            block.setVisible(needle in text)

    # -- playback sync ---------------------------------------------------

    def set_active(self, index: int) -> None:
        """Highlight the caption currently being spoken (§ sync review)."""
        if index == self._active:
            return
        if 0 <= self._active < len(self._blocks):
            self._blocks[self._active].set_active(False)
        self._active = index
        if 0 <= index < len(self._blocks):
            self._blocks[index].set_active(True)

    def scroll_to(self, index: int) -> None:
        if 0 <= index < len(self._blocks):
            self._scroll.ensureWidgetVisible(self._blocks[index], 0, 120)

    def follow_playhead(self, milliseconds: int) -> None:
        for index, segment in enumerate(self._state.segments):
            if segment.start_ms <= milliseconds < segment.end_ms:
                if index != self._active:
                    self.set_active(index)
                    self.scroll_to(index)
                return
