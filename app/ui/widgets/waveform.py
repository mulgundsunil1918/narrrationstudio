"""Waveform and timeline views.

The timeline draws three lanes — captions, narration segments and the audio —
so the central idea of the product is visible at a glance: caption boundaries
fall *inside* a continuous narration block.
"""

from __future__ import annotations

import numpy as np
from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from app.ui.theme import CAPTION, palette
from app.ui.widgets.common import clock

PEAK_BUCKETS = 1800


class WaveformView(QWidget):
    """Peak-envelope waveform with a playhead the user can scrub."""

    scrubbed = Signal(int)  # milliseconds

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(96)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._peaks: np.ndarray | None = None
        self._duration_ms = 0
        self._position_ms = 0
        self._pending = False

    # -- data ------------------------------------------------------------

    def set_audio(self, audio: np.ndarray | None, sample_rate: int) -> None:
        """Reduce the waveform to a fixed number of peak buckets for drawing."""
        if audio is None or len(audio) == 0 or sample_rate <= 0:
            self._peaks = None
            self._duration_ms = 0
            self.update()
            return

        self._duration_ms = int(round(len(audio) / sample_rate * 1000))
        buckets = min(PEAK_BUCKETS, max(1, len(audio)))
        usable = (len(audio) // buckets) * buckets
        if usable == 0:
            self._peaks = np.abs(audio).astype(np.float32)
        else:
            reshaped = np.abs(audio[:usable]).reshape(buckets, -1)
            self._peaks = reshaped.max(axis=1).astype(np.float32)
        self._pending = False
        self.update()

    def set_pending(self, pending: bool) -> None:
        """Show a placeholder while audio is still being produced."""
        self._pending = pending
        self.update()

    def set_position(self, milliseconds: int) -> None:
        self._position_ms = max(0, milliseconds)
        self.update()

    @property
    def duration_ms(self) -> int:
        return self._duration_ms

    # -- interaction -----------------------------------------------------

    def mousePressEvent(self, event) -> None:
        self._seek_to(event.position().x())

    def mouseMoveEvent(self, event) -> None:
        if event.buttons() & Qt.MouseButton.LeftButton:
            self._seek_to(event.position().x())

    def _seek_to(self, x: float) -> None:
        if self._duration_ms <= 0 or self.width() <= 0:
            return
        fraction = min(1.0, max(0.0, x / self.width()))
        self.scrubbed.emit(int(fraction * self._duration_ms))

    # -- painting --------------------------------------------------------

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        current = palette()
        area = self.rect()

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(current.surface_alt))
        painter.drawRoundedRect(QRectF(area), 10, 10)

        if self._peaks is None:
            message = "Generating…" if self._pending else "No audio yet"
            painter.setPen(QPen(QColor(current.text_faint)))
            font = painter.font()
            font.setPointSize(CAPTION)
            painter.setFont(font)
            painter.drawText(area, Qt.AlignmentFlag.AlignCenter, message)
            return

        middle = area.center().y()
        half = area.height() / 2 - 10
        count = len(self._peaks)
        step = area.width() / count

        played_until = (
            self._position_ms / self._duration_ms * area.width()
            if self._duration_ms
            else 0
        )

        painter.setPen(Qt.PenStyle.NoPen)
        for index, peak in enumerate(self._peaks):
            x = index * step
            height = max(1.0, float(peak) * half)
            colour = current.accent if x <= played_until else current.border_strong
            painter.setBrush(QColor(colour))
            painter.drawRect(QRectF(x, middle - height, max(1.0, step * 0.8), height * 2))

        if self._duration_ms:
            pen = QPen(QColor(current.text))
            pen.setWidth(2)
            painter.setPen(pen)
            painter.drawLine(int(played_until), area.top() + 4, int(played_until), area.bottom() - 4)


class TimelineView(QWidget):
    """Three stacked lanes: captions, narration segments, and the audio bed."""

    caption_clicked = Signal(int)

    LANE_HEIGHT = 26
    LANE_GAP = 8
    LABEL_WIDTH = 96

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(150)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(150)
        self._captions: list[tuple[int, int]] = []
        self._segments: list[tuple[int, int, bool]] = []
        self._duration_ms = 0
        self._position_ms = 0
        self._active_caption = -1

    def set_data(
        self,
        captions: list[tuple[int, int]],
        segments: list[tuple[int, int, bool]],
        duration_ms: int,
    ) -> None:
        self._captions = captions
        self._segments = segments
        self._duration_ms = max(1, duration_ms)
        self.update()

    def set_position(self, milliseconds: int) -> None:
        self._position_ms = milliseconds
        active = -1
        for index, (start, end) in enumerate(self._captions):
            if start <= milliseconds < end:
                active = index
                break
        if active != self._active_caption:
            self._active_caption = active
        self.update()

    def _x(self, milliseconds: int, width: float) -> float:
        return self.LABEL_WIDTH + milliseconds / self._duration_ms * width

    def mousePressEvent(self, event) -> None:
        width = self.width() - self.LABEL_WIDTH - 8
        if width <= 0 or not self._captions:
            return
        fraction = (event.position().x() - self.LABEL_WIDTH) / width
        milliseconds = int(min(1.0, max(0.0, fraction)) * self._duration_ms)
        for index, (start, end) in enumerate(self._captions):
            if start <= milliseconds < end:
                self.caption_clicked.emit(index)
                return

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        current = palette()
        width = self.width() - self.LABEL_WIDTH - 8
        if width <= 0:
            return

        font = painter.font()
        font.setPointSize(CAPTION - 1)
        painter.setFont(font)

        lanes = [("CAPTIONS", 12), ("NARRATION", 12 + self.LANE_HEIGHT + self.LANE_GAP),
                 ("AUDIO", 12 + 2 * (self.LANE_HEIGHT + self.LANE_GAP))]

        for name, top in lanes:
            painter.setPen(QPen(QColor(current.text_faint)))
            painter.drawText(
                QRectF(0, top, self.LABEL_WIDTH - 10, self.LANE_HEIGHT),
                int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                name,
            )
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(current.surface_alt))
            painter.drawRoundedRect(
                QRectF(self.LABEL_WIDTH, top, width, self.LANE_HEIGHT), 5, 5
            )

        # Captions: one block per subtitle, with visible boundaries.
        top = lanes[0][1]
        for index, (start, end) in enumerate(self._captions):
            x = self._x(start, width)
            span = max(1.5, (end - start) / self._duration_ms * width)
            active = index == self._active_caption
            painter.setBrush(QColor(current.accent if active else current.border_strong))
            painter.drawRoundedRect(QRectF(x + 0.5, top + 5, span - 1, self.LANE_HEIGHT - 10), 3, 3)

        # Narration: continuous blocks that span several captions.
        top = lanes[1][1]
        for start, end, forced in self._segments:
            x = self._x(start, width)
            span = max(2.0, (end - start) / self._duration_ms * width)
            painter.setBrush(QColor(current.warning if forced else current.success))
            painter.drawRoundedRect(QRectF(x + 0.5, top + 4, span - 1, self.LANE_HEIGHT - 8), 4, 4)

        # Audio bed: the whole timeline, showing that narration is continuous.
        top = lanes[2][1]
        painter.setBrush(QColor(current.accent_soft))
        painter.drawRoundedRect(QRectF(self.LABEL_WIDTH, top + 6, width, self.LANE_HEIGHT - 12), 4, 4)

        # Playhead across every lane.
        if self._duration_ms:
            x = self._x(self._position_ms, width)
            pen = QPen(QColor(current.text))
            pen.setWidth(2)
            painter.setPen(pen)
            painter.drawLine(int(x), 8, int(x), lanes[2][1] + self.LANE_HEIGHT + 2)

        # Time ruler.
        painter.setPen(QPen(QColor(current.text_faint)))
        ruler_top = lanes[2][1] + self.LANE_HEIGHT + 6
        for fraction in (0.0, 0.25, 0.5, 0.75, 1.0):
            milliseconds = int(self._duration_ms * fraction)
            x = self.LABEL_WIDTH + fraction * width
            alignment = Qt.AlignmentFlag.AlignLeft
            if fraction == 1.0:
                alignment = Qt.AlignmentFlag.AlignRight
                x -= 60
            elif fraction > 0:
                x -= 30
                alignment = Qt.AlignmentFlag.AlignCenter
            painter.drawText(
                QRectF(x, ruler_top, 60, 14), int(alignment), clock(milliseconds)
            )
