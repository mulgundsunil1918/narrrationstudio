"""Reusable UI primitives.

Small, composable pieces so no screen has to hand-roll a card, a pill or a
labelled slider. Everything reads its colours from :mod:`app.ui.theme` at build
time so a palette change is one place.
"""

from __future__ import annotations

from PySide6.QtCore import (
    QEasingCurve,
    QPropertyAnimation,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from app.ui.theme import BODY, CAPTION, SMALL, palette, tone, ui_font


def label(text: str, role: str = "Body", wrap: bool = False) -> QLabel:
    """A themed label. ``role`` maps to an object name in the stylesheet."""
    widget = QLabel(text)
    widget.setObjectName(role)
    widget.setWordWrap(wrap)
    widget.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
    if wrap:
        # Let the label give back height instead of demanding a full-width line.
        widget.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        widget.setMinimumWidth(120)
    return widget


def heading(text: str) -> QLabel:
    return label(text, "Heading")


def title(text: str) -> QLabel:
    return label(text, "Title")


def display(text: str) -> QLabel:
    return label(text, "Display")


def muted(text: str, wrap: bool = False) -> QLabel:
    return label(text, "Muted", wrap)


def caption(text: str, wrap: bool = False) -> QLabel:
    return label(text, "Caption", wrap)


def section_label(text: str) -> QLabel:
    return label(text.upper(), "SectionLabel")


class HeightForWidthMixin:
    """Lets a container grow to fit word-wrapped children.

    A ``QLabel`` with word wrap reports a wide, one-line ``sizeHint``. Without
    propagating ``heightForWidth`` upward, a container sizes itself from that
    hint, then the label wraps to two lines inside a box that was never made
    tall enough — which is what makes stacked cards paint over each other.
    """

    def _enable_height_for_width(self) -> None:
        policy = self.sizePolicy()
        policy.setHeightForWidth(True)
        policy.setVerticalPolicy(QSizePolicy.Policy.Minimum)
        self.setSizePolicy(policy)

    def hasHeightForWidth(self) -> bool:
        # Only claim it when the layout can actually answer. Claiming it
        # unconditionally makes Qt ask, get -1 back, and collapse the widget.
        layout = self.layout()
        return bool(layout is not None and layout.hasHeightForWidth())

    def heightForWidth(self, width: int) -> int:
        layout = self.layout()
        if layout is not None and layout.hasHeightForWidth():
            height = layout.heightForWidth(width)
            if height > 0:
                return height
        return self.sizeHint().height()

    def paintEvent(self, event) -> None:
        # A QWidget/QFrame subclass does not paint its stylesheet background
        # unless it asks the style to; without this, scrolled content smears.
        from PySide6.QtWidgets import QStyle, QStyleOption

        option = QStyleOption()
        option.initFrom(self)
        painter = QPainter(self)
        self.style().drawPrimitive(
            QStyle.PrimitiveElement.PE_Widget, option, painter, self
        )
        super().paintEvent(event)


class Card(HeightForWidthMixin, QFrame):
    """A surface with a border and rounded corners."""

    def __init__(self, quiet: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("CardQuiet" if quiet else "Card")
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(20, 18, 20, 20)
        self.body.setSpacing(14)
        self._enable_height_for_width()

    def add(self, widget: QWidget, stretch: int = 0) -> QWidget:
        self.body.addWidget(widget, stretch)
        return widget

    def add_layout(self, layout) -> None:
        self.body.addLayout(layout)


class Divider(QFrame):
    def __init__(self, vertical: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Divider")
        if vertical:
            self.setFixedWidth(1)
            self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        else:
            self.setFixedHeight(1)
            self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)


class Pill(QLabel):
    """A small status badge: ● Local processing, ✓ Synchronized, ⚠ 3 warnings."""

    def __init__(self, text: str, kind: str = "neutral", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.set_kind(kind)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)

    def set_kind(self, kind: str) -> None:
        foreground, background = tone(kind)
        self.setStyleSheet(
            f"color: {foreground}; background-color: {background};"
            f" border-radius: 11px; padding: 4px 11px;"
            f" font-size: {CAPTION}px; font-weight: 600;"
        )

    def set_status(self, text: str, kind: str) -> None:
        self.setText(text)
        self.set_kind(kind)


class PrimaryButton(QPushButton):
    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setObjectName("Primary")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(38)


class GhostButton(QPushButton):
    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setObjectName("Ghost")
        self.setCursor(Qt.CursorShape.PointingHandCursor)


class SecondaryButton(QPushButton):
    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(38)


class Metric(QWidget):
    """A big number with a small label beneath -- used across summary rows."""

    def __init__(self, value: str, name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(3)
        self._value = label(value, "Metric")
        self._name = label(name, "MetricLabel")
        column.addWidget(self._value)
        column.addWidget(self._name)

    def set_value(self, value: str) -> None:
        self._value.setText(value)

    def set_tone(self, kind: str) -> None:
        foreground, _ = tone(kind)
        self._value.setStyleSheet(f"color: {foreground}; font-size: 24px; font-weight: 700;")


class LabeledSlider(QWidget):
    """A slider with a name on the left and its live value on the right."""

    valueChanged = Signal(int)

    def __init__(
        self,
        name: str,
        minimum: int,
        maximum: int,
        value: int,
        suffix: str = "%",
        scale: float = 1.0,
        decimals: int = 0,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._suffix = suffix
        self._scale = scale
        self._decimals = decimals

        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(6)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        self._name = label(name, "Muted")
        self._value = label("", "Muted")
        self._value.setAlignment(Qt.AlignmentFlag.AlignRight)
        header.addWidget(self._name)
        header.addStretch(1)
        header.addWidget(self._value)
        column.addLayout(header)

        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(minimum, maximum)
        self.slider.setValue(value)
        self.slider.valueChanged.connect(self._on_change)
        column.addWidget(self.slider)

        self._render(value)

    def _on_change(self, value: int) -> None:
        self._render(value)
        self.valueChanged.emit(value)

    def _render(self, value: int) -> None:
        scaled = value * self._scale
        self._value.setText(f"{scaled:.{self._decimals}f}{self._suffix}")

    def value(self) -> int:
        return self.slider.value()

    def setValue(self, value: int) -> None:
        self.slider.setValue(value)


class Segmented(QWidget):
    """A row of mutually exclusive chips -- lighter than a combo for 2-4 options."""

    changed = Signal(str)

    def __init__(
        self,
        options: list[tuple[str, str]],
        initial: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(7)
        self._buttons: dict[str, QPushButton] = {}
        self._current = ""
        for key, text in options:
            button = QPushButton(text)
            button.setObjectName("Chip")
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _checked, k=key: self.select(k))
            row.addWidget(button)
            self._buttons[key] = button
        row.addStretch(1)
        if options:
            # Setting the initial state must not look like a user choice, or the
            # first chip would silently overwrite whatever the caller configured.
            self.select(initial or options[0][0], emit=False)

    def select(self, key: str, emit: bool = True) -> None:
        if key not in self._buttons:
            return
        for name, button in self._buttons.items():
            button.setChecked(name == key)
        self._current = key
        if emit:
            self.changed.emit(key)

    def current(self) -> str:
        return self._current


class Field(QWidget):
    """A labelled control, stacked vertically."""

    def __init__(self, name: str, control: QWidget, hint: str = "", parent=None) -> None:
        super().__init__(parent)
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(6)
        column.addWidget(label(name, "Muted"))
        column.addWidget(control)
        if hint:
            column.addWidget(caption(hint, wrap=True))
        self.control = control


class EmptyState(QWidget):
    """A helpful placeholder with a single obvious action."""

    action = Signal()

    def __init__(
        self,
        icon: str,
        headline: str,
        body: str,
        action_text: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        column = QVBoxLayout(self)
        column.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column.setSpacing(10)

        glyph = QLabel(icon)
        glyph.setAlignment(Qt.AlignmentFlag.AlignCenter)
        glyph.setStyleSheet("font-size: 38px;")
        column.addWidget(glyph)

        top = label(headline, "Title")
        top.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column.addWidget(top)

        text = muted(body, wrap=True)
        text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        text.setMaximumWidth(420)
        column.addWidget(text, alignment=Qt.AlignmentFlag.AlignCenter)

        if action_text:
            column.addSpacing(6)
            button = PrimaryButton(action_text)
            button.setFixedWidth(200)
            button.clicked.connect(self.action)
            column.addWidget(button, alignment=Qt.AlignmentFlag.AlignCenter)


class Toast(QFrame):
    """A transient status message that fades in over the workspace."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        self.setVisible(False)
        row = QHBoxLayout(self)
        row.setContentsMargins(15, 11, 15, 11)
        row.setSpacing(10)
        self._icon = QLabel("")
        self._text = label("", "Body")
        row.addWidget(self._icon)
        row.addWidget(self._text)

        self._effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._effect)
        self._fade = QPropertyAnimation(self._effect, b"opacity", self)
        self._fade.setDuration(180)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.dismiss)

    def show_message(self, text: str, kind: str = "info", milliseconds: int = 3200) -> None:
        glyphs = {"info": "›", "success": "✓", "warning": "⚠", "error": "✕"}
        foreground, _ = tone(kind)
        self._icon.setText(glyphs.get(kind, "›"))
        self._icon.setStyleSheet(f"color: {foreground}; font-size: 14px; font-weight: 700;")
        self._text.setText(text)
        self.adjustSize()
        self._reposition()
        self.setVisible(True)
        self.raise_()
        self._fade.stop()
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._fade.start()
        self._timer.start(milliseconds)

    def dismiss(self) -> None:
        self._fade.stop()
        self._fade.setStartValue(self._effect.opacity())
        self._fade.setEndValue(0.0)
        self._fade.finished.connect(self._hide_once)
        self._fade.start()

    def _hide_once(self) -> None:
        try:
            self._fade.finished.disconnect(self._hide_once)
        except (RuntimeError, TypeError):
            pass
        self.setVisible(False)

    def _reposition(self) -> None:
        if not self.parentWidget():
            return
        area = self.parentWidget().rect()
        self.move(
            area.center().x() - self.width() // 2,
            area.bottom() - self.height() - 28,
        )


class Spinner(QWidget):
    """A minimal indeterminate activity indicator."""

    def __init__(self, diameter: int = 18, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._angle = 0
        self._diameter = diameter
        self.setFixedSize(QSize(diameter, diameter))
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._advance)

    def start(self) -> None:
        if not self._timer.isActive():
            self._timer.start(40)
        self.setVisible(True)

    def stop(self) -> None:
        self._timer.stop()
        self.setVisible(False)

    def _advance(self) -> None:
        self._angle = (self._angle + 24) % 360
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        current = palette()
        pen = QPen(QColor(current.accent))
        pen.setWidth(2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        margin = 2
        rect = self.rect().adjusted(margin, margin, -margin, -margin)
        painter.drawArc(rect, -self._angle * 16, 100 * 16)


def row(*widgets: QWidget, spacing: int = 10, stretch_at: int | None = None) -> QHBoxLayout:
    """Convenience horizontal layout."""
    layout = QHBoxLayout()
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(spacing)
    for index, widget in enumerate(widgets):
        if stretch_at is not None and index == stretch_at:
            layout.addStretch(1)
        layout.addWidget(widget)
    if stretch_at is not None and stretch_at >= len(widgets):
        layout.addStretch(1)
    return layout


def clear_layout(layout, keep_last: int = 0) -> None:
    """Remove and destroy every widget in ``layout``.

    ``deleteLater`` alone is not enough: the widget stays parented until the
    event loop runs the deferred delete, so it keeps painting over whatever
    replaces it. Unparenting first removes it from the hierarchy immediately.
    """
    while layout.count() > keep_last:
        item = layout.takeAt(0)
        if item is None:
            break
        widget = item.widget()
        if widget is not None:
            widget.setParent(None)
            widget.deleteLater()
            continue
        child = item.layout()
        if child is not None:
            clear_layout(child)
            child.deleteLater()


def clock(milliseconds: int) -> str:
    """Format a duration as m:ss or h:mm:ss."""
    seconds = int(milliseconds) // 1000
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"
