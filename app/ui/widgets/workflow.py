"""The guided workflow: a numbered step rail and a Back/Continue footer.

Screens on their own are a filing cabinet — the user has to already know the
order. These two widgets sit above and below the workspace and make the path
explicit: which step you are on, which are done, what this step is for, and what
happens when you press Continue.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget

from app.ui.theme import CAPTION, palette
from app.ui.widgets.common import (
    GhostButton,
    PrimaryButton,
    SecondaryButton,
    caption,
    label,
    muted,
)


@dataclass(frozen=True)
class Step:
    key: str
    number: int
    name: str
    #: What this step is for, shown under the heading.
    purpose: str
    #: What pressing Continue does next.
    next_label: str


STEPS: tuple[Step, ...] = (
    Step("script", 1, "Script",
         "Read through the words that will be spoken and fix anything wrong.",
         "Choose a Voice"),
    Step("voice", 2, "Voice",
         "Pick who reads your script. Press Preview to hear a voice first.",
         "Set up Narration"),
    Step("narration", 3, "Narration",
         "Decide how continuous the speech should sound.",
         "Go to Generate"),
    Step("generate", 4, "Generate",
         "Check everything is ready, then create the narration.",
         "Review the Result"),
    Step("review", 5, "Preview",
         "Play the whole narration and confirm it lines up before you export.",
         "Export Your Files"),
    Step("export", 6, "Export",
         "Save the narration, and your subtitles if you edited them.",
         ""),
)

BY_KEY = {step.key: step for step in STEPS}
ORDER = [step.key for step in STEPS]


class StepRail(QWidget):
    """The numbered progress rail across the top of the workspace."""

    step_clicked = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("TopBar")
        self.setFixedHeight(62)
        self._current = "script"
        self._done: set[str] = set()
        self._enabled: set[str] = set(ORDER)

        row = QHBoxLayout(self)
        row.setContentsMargins(28, 0, 28, 0)
        row.setSpacing(0)

        self._pips: dict[str, _StepPip] = {}
        for index, step in enumerate(STEPS):
            pip = _StepPip(step)
            pip.clicked.connect(self.step_clicked)
            row.addWidget(pip)
            self._pips[step.key] = pip
            if index < len(STEPS) - 1:
                row.addWidget(_Connector(), 1)

    def set_state(self, current: str, done: set[str], enabled: set[str]) -> None:
        self._current, self._done, self._enabled = current, done, enabled
        for key, pip in self._pips.items():
            pip.set_state(
                current=key == current,
                done=key in done and key != current,
                enabled=key in enabled,
            )


class _StepPip(QWidget):
    """One numbered step in the rail."""

    clicked = Signal(str)

    def __init__(self, step: Step, parent=None) -> None:
        super().__init__(parent)
        self._step = step
        self._current = False
        self._done = False
        self._enabled = True
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(9)

        self._badge = label("")
        self._badge.setFixedSize(26, 26)
        self._badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        row.addWidget(self._badge)

        self._name = label(step.name, "Body")
        row.addWidget(self._name)
        self.set_state(False, False, True)

    def mousePressEvent(self, event) -> None:
        if self._enabled:
            self.clicked.emit(self._step.key)

    def set_state(self, current: bool, done: bool, enabled: bool) -> None:
        self._current, self._done, self._enabled = current, done, enabled
        p = palette()

        if not enabled:
            badge_fg, badge_bg, border = p.text_faint, "transparent", p.border
            name_colour, weight = p.text_faint, 400
        elif current:
            badge_fg, badge_bg, border = p.accent_text, p.accent, p.accent
            name_colour, weight = p.text, 600
        elif done:
            badge_fg, badge_bg, border = p.success, p.success_soft, p.success
            name_colour, weight = p.text_dim, 500
        else:
            badge_fg, badge_bg, border = p.text_dim, "transparent", p.border_strong
            name_colour, weight = p.text_dim, 400

        self._badge.setText("✓" if done and not current else str(self._step.number))
        self._badge.setStyleSheet(
            f"color: {badge_fg}; background-color: {badge_bg};"
            f" border: 1px solid {border}; border-radius: 13px;"
            f" font-size: {CAPTION}px; font-weight: 700;"
        )
        self._name.setStyleSheet(
            f"color: {name_colour}; font-size: 13px; font-weight: {weight};"
        )
        self.setToolTip(self._step.purpose if enabled else "Import a script first")


class _Connector(QWidget):
    """The hairline between two steps."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumWidth(16)
        self.setFixedHeight(62)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        pen = QPen(QColor(palette().border_strong))
        pen.setWidth(1)
        painter.setPen(pen)
        middle = self.height() // 2
        painter.drawLine(6, middle, self.width() - 6, middle)


class StepFooter(QWidget):
    """Back / Continue, with a plain-language description of the current step."""

    back = Signal()
    forward = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("BottomBar")
        self.setFixedHeight(76)

        row = QHBoxLayout(self)
        row.setContentsMargins(28, 0, 28, 0)
        row.setSpacing(14)

        self._back = SecondaryButton("←  Back")
        self._back.clicked.connect(self.back)
        row.addWidget(self._back)

        column = QVBoxLayout()
        column.setSpacing(1)
        self._heading = label("", "Body")
        self._purpose = caption("")
        self._purpose.setWordWrap(False)
        column.addWidget(self._heading)
        column.addWidget(self._purpose)
        row.addLayout(column, 1)

        self._hint = caption("")
        self._hint.setAlignment(Qt.AlignmentFlag.AlignRight)
        row.addWidget(self._hint)

        self._next = PrimaryButton("Continue")
        self._next.setMinimumWidth(190)
        self._next.clicked.connect(self.forward)
        row.addWidget(self._next)

    def set_step(
        self,
        step: Step,
        can_advance: bool = True,
        blocked_reason: str = "",
        is_first: bool = False,
        next_label: str = "",
    ) -> None:
        self._heading.setText(f"Step {step.number} of {len(STEPS)} — {step.name}")
        self._purpose.setText(step.purpose)
        self._purpose.setToolTip(step.purpose)
        self._back.setEnabled(not is_first)

        label_text = next_label or step.next_label
        if not label_text:
            self._next.setText("Finish")
            self._next.setEnabled(True)
            self._hint.setText("You're done — your files are saved.")
            return

        self._next.setText(label_text if next_label else f"{label_text}  →")
        self._next.setEnabled(can_advance)
        self._hint.setText(blocked_reason if not can_advance else "")
        self._hint.setStyleSheet(
            f"color: {palette().warning}; font-size: {CAPTION}px;"
            if not can_advance
            else f"color: {palette().text_faint}; font-size: {CAPTION}px;"
        )
