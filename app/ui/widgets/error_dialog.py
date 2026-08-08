"""The single place a failure is shown to the user.

Every error in the app arrives here as an :class:`OperationError` and is
rendered as: what happened, why, what to do next — plus a disclosure holding the
raw traceback, command and output for debugging. Raw Python never reaches the
main surface.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.core.status import ACTION_LABELS, OperationError, Severity
from app.ui.theme import mono_font, palette, tone
from app.ui.widgets.common import (
    Card,
    GhostButton,
    PrimaryButton,
    SecondaryButton,
    label,
    muted,
    section_label,
)


class ErrorDialog(QDialog):
    """Explains a failure and offers the actions that make sense for it."""

    #: Emitted with an action key ("retry", "change_voice", …) when chosen.
    action_chosen = Signal(str)

    def __init__(self, error: OperationError, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._error = error
        self._chosen = ""

        is_warning = error.severity is Severity.WARNING
        self.setWindowTitle("Warning" if is_warning else "Something went wrong")
        self.setMinimumWidth(560)
        self.setModal(True)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 22, 24, 22)
        outer.setSpacing(18)

        outer.addLayout(self._build_header(error, is_warning))
        outer.addWidget(self._build_body(error))

        self._details = QPlainTextEdit(error.technical_report())
        self._details.setReadOnly(True)
        self._details.setFont(mono_font(11))
        self._details.setMinimumHeight(220)
        self._details.setVisible(False)
        outer.addWidget(self._details)

        outer.addLayout(self._build_actions(error))

    # -- construction ----------------------------------------------------

    def _build_header(self, error: OperationError, is_warning: bool) -> QHBoxLayout:
        kind = "warning" if is_warning else "error"
        foreground, background = tone(kind)

        glyph = label("⚠" if is_warning else "✕")
        glyph.setFixedSize(38, 38)
        glyph.setAlignment(Qt.AlignmentFlag.AlignCenter)
        glyph.setStyleSheet(
            f"color: {foreground}; background-color: {background};"
            f" border-radius: 19px; font-size: 17px; font-weight: 700;"
        )

        headline = label(error.user_message, "Title", wrap=True)

        column = QVBoxLayout()
        column.setSpacing(3)
        column.addWidget(headline)
        column.addWidget(muted(f"Error code {error.code.value}"))

        header = QHBoxLayout()
        header.setSpacing(14)
        header.addWidget(glyph, alignment=Qt.AlignmentFlag.AlignTop)
        header.addLayout(column, 1)
        return header

    def _build_body(self, error: OperationError) -> QWidget:
        card = Card(quiet=True)
        card.body.setSpacing(13)

        if error.reason:
            card.add(section_label("Why this happened"))
            card.add(label(error.reason, "Body", wrap=True))
        if error.recommended_action:
            card.add(section_label("What you can do"))
            card.add(label(error.recommended_action, "Body", wrap=True))
        if error.segment is not None:
            card.add(muted(f"Affected section: {error.segment}"))
        if not error.reason and not error.recommended_action:
            card.add(muted("No further information is available.", wrap=True))
        return card

    def _build_actions(self, error: OperationError) -> QHBoxLayout:
        actions = QHBoxLayout()
        actions.setSpacing(9)

        self._details_button = GhostButton("View Technical Details")
        self._details_button.clicked.connect(self._toggle_details)
        actions.addWidget(self._details_button)

        copy_button = GhostButton("Copy")
        copy_button.setToolTip("Copy the technical report to the clipboard")
        copy_button.clicked.connect(self._copy_details)
        actions.addWidget(copy_button)

        actions.addStretch(1)

        close = SecondaryButton("Close")
        close.clicked.connect(self.reject)
        actions.addWidget(close)

        primary_added = False
        for key in error.actions:
            if key == "details":
                continue
            text = ACTION_LABELS.get(key, key.replace("_", " ").title())
            button = PrimaryButton(text) if not primary_added else SecondaryButton(text)
            primary_added = True
            button.clicked.connect(lambda _checked=False, k=key: self._choose(k))
            actions.addWidget(button)
        return actions

    # -- behaviour -------------------------------------------------------

    def _toggle_details(self) -> None:
        showing = not self._details.isVisible()
        self._details.setVisible(showing)
        self._details_button.setText(
            "Hide Technical Details" if showing else "View Technical Details"
        )
        self.adjustSize()

    def _copy_details(self) -> None:
        clipboard = QGuiApplication.clipboard()
        if clipboard:
            clipboard.setText(self._error.technical_report())

    def _choose(self, key: str) -> None:
        self._chosen = key
        self.action_chosen.emit(key)
        self.accept()

    @property
    def chosen_action(self) -> str:
        return self._chosen


def show_error(error: OperationError, parent: QWidget | None = None) -> str:
    """Present ``error`` and return the action the user picked ("" if none)."""
    dialog = ErrorDialog(error, parent)
    dialog.exec()
    return dialog.chosen_action
