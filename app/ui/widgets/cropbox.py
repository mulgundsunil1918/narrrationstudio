"""A crop rectangle you drag on the picture itself.

Numbers are the wrong interface for "keep that part": the person choosing a
crop is looking at the frame, not thinking in coordinates. So the frame is
shown and the rectangle lives on it — drag the middle to move it, pull an edge
or a corner to resize it, and everything outside dims so what is being thrown
away is never a surprise.

The rectangle is held as fractions of the frame (a :class:`FreeCrop`), so what
is drawn on this scaled-down preview means exactly the same thing on the
full-resolution video at export time.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen
from PySide6.QtWidgets import QWidget

from app.video.crop import FreeCrop

logger = logging.getLogger(__name__)

#: How close (in widget pixels) counts as grabbing an edge or corner.
GRAB = 12.0

#: Hit zones, and which edges each one drags.
_ZONES: dict[str, tuple[bool, bool, bool, bool]] = {
    # name: (left, top, right, bottom)
    "topleft": (True, True, False, False),
    "topright": (False, True, True, False),
    "bottomleft": (True, False, False, True),
    "bottomright": (False, False, True, True),
    "left": (True, False, False, False),
    "top": (False, True, False, False),
    "right": (False, False, True, False),
    "bottom": (False, False, False, True),
}

_CURSORS = {
    "move": Qt.CursorShape.SizeAllCursor,
    "left": Qt.CursorShape.SizeHorCursor,
    "right": Qt.CursorShape.SizeHorCursor,
    "top": Qt.CursorShape.SizeVerCursor,
    "bottom": Qt.CursorShape.SizeVerCursor,
    "topleft": Qt.CursorShape.SizeFDiagCursor,
    "bottomright": Qt.CursorShape.SizeFDiagCursor,
    "topright": Qt.CursorShape.SizeBDiagCursor,
    "bottomleft": Qt.CursorShape.SizeBDiagCursor,
}


class CropBox(QWidget):
    """Shows a frame and lets the user carve a rectangle out of it."""

    #: Fired continuously while dragging — cheap listeners only (a size label).
    edited = Signal(object)      # FreeCrop
    #: Fired when the mouse is released — the moment to redo expensive work.
    committed = Signal(object)   # FreeCrop

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._image: QImage | None = None
        self._crop = FreeCrop()
        self._mode: str | None = None
        self._press = QPointF()
        self._origin = self._crop
        self.setMinimumHeight(230)
        self.setMouseTracking(True)   # cursor feedback needs moves without a button

    # -- data ------------------------------------------------------------

    def set_frame(self, image: QImage) -> None:
        self._image = image
        self.update()

    def set_crop(self, crop: FreeCrop) -> None:
        self._crop = crop.normalised()
        self.update()

    def crop(self) -> FreeCrop:
        return self._crop

    # -- geometry --------------------------------------------------------

    def _image_rect(self) -> QRectF:
        """Where the frame sits inside the widget, aspect kept, centred."""
        if self._image is None or self._image.width() == 0:
            return QRectF(0, 0, self.width(), self.height())
        scale = min(
            self.width() / self._image.width(), self.height() / self._image.height()
        )
        width = self._image.width() * scale
        height = self._image.height() * scale
        return QRectF(
            (self.width() - width) / 2, (self.height() - height) / 2, width, height
        )

    def _crop_rect(self) -> QRectF:
        """The crop rectangle in widget coordinates."""
        area = self._image_rect()
        spec = self._crop.normalised()
        return QRectF(
            area.left() + spec.left * area.width(),
            area.top() + spec.top * area.height(),
            spec.width * area.width(),
            spec.height * area.height(),
        )

    def _to_fractions(self, point: QPointF) -> tuple[float, float]:
        area = self._image_rect()
        if area.width() <= 0 or area.height() <= 0:
            return 0.0, 0.0
        return (
            min(1.0, max(0.0, (point.x() - area.left()) / area.width())),
            min(1.0, max(0.0, (point.y() - area.top()) / area.height())),
        )

    def _zone_at(self, point: QPointF) -> str | None:
        """What the mouse is over: an edge name, "move", "new", or None."""
        rect = self._crop_rect()
        near_left = abs(point.x() - rect.left()) <= GRAB
        near_right = abs(point.x() - rect.right()) <= GRAB
        near_top = abs(point.y() - rect.top()) <= GRAB
        near_bottom = abs(point.y() - rect.bottom()) <= GRAB
        within_x = rect.left() - GRAB <= point.x() <= rect.right() + GRAB
        within_y = rect.top() - GRAB <= point.y() <= rect.bottom() + GRAB

        if near_top and near_left:
            return "topleft"
        if near_top and near_right:
            return "topright"
        if near_bottom and near_left:
            return "bottomleft"
        if near_bottom and near_right:
            return "bottomright"
        if near_left and within_y:
            return "left"
        if near_right and within_y:
            return "right"
        if near_top and within_x:
            return "top"
        if near_bottom and within_x:
            return "bottom"
        if rect.contains(point):
            return "move"
        if self._image_rect().contains(point):
            return "new"
        return None

    # -- interaction, kept apart from the events so tests can drive it ----

    def begin(self, point: QPointF) -> None:
        self._mode = self._zone_at(point)
        self._press = point
        self._origin = self._crop.normalised()
        if self._mode == "new":
            fx, fy = self._to_fractions(point)
            self._crop = FreeCrop(fx, fy, FreeCrop.MIN_FRACTION, FreeCrop.MIN_FRACTION)
            # From here it behaves like pulling the bottom-right corner — but
            # the maths starts from a zero-size rectangle at the press point,
            # so the drawn edge lands exactly under the cursor instead of a
            # seed-size beyond it. The minimum is enforced on the result.
            self._origin = FreeCrop(fx, fy, 0.0, 0.0)
            self._mode = "bottomright"
        self.update()

    def drag(self, point: QPointF) -> None:
        if self._mode is None:
            return
        area = self._image_rect()
        if area.width() <= 0 or area.height() <= 0:
            return
        dx = (point.x() - self._press.x()) / area.width()
        dy = (point.y() - self._press.y()) / area.height()
        origin = self._origin

        if self._mode == "move":
            self._crop = FreeCrop(
                origin.left + dx, origin.top + dy, origin.width, origin.height
            ).normalised()
        elif self._mode in _ZONES:
            self._crop = _resize(origin, self._mode, dx, dy)
        self.edited.emit(self._crop)
        self.update()

    def finish(self) -> None:
        if self._mode is not None:
            self._mode = None
            self.committed.emit(self._crop)
        self.update()

    # -- events ----------------------------------------------------------

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.begin(event.position())

    def mouseMoveEvent(self, event) -> None:
        if self._mode is not None:
            self.drag(event.position())
            return
        zone = self._zone_at(event.position())
        self.setCursor(_CURSORS.get(zone or "", Qt.CursorShape.CrossCursor)
                       if zone else Qt.CursorShape.ArrowCursor)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.finish()

    # -- painting --------------------------------------------------------

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        area = self._image_rect()

        if self._image is not None:
            painter.drawImage(area, self._image)
        else:
            painter.fillRect(area, QColor(70, 70, 74))

        rect = self._crop_rect()

        # Dim what will be thrown away, leave what is kept at full strength.
        shade = QColor(0, 0, 0, 130)
        painter.fillRect(QRectF(area.left(), area.top(), area.width(),
                                rect.top() - area.top()), shade)
        painter.fillRect(QRectF(area.left(), rect.bottom(), area.width(),
                                area.bottom() - rect.bottom()), shade)
        painter.fillRect(QRectF(area.left(), rect.top(),
                                rect.left() - area.left(), rect.height()), shade)
        painter.fillRect(QRectF(rect.right(), rect.top(),
                                area.right() - rect.right(), rect.height()), shade)

        painter.setPen(QPen(QColor(255, 255, 255, 230), 1.6))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(rect)

        # Rule-of-thirds guides, faint, only while actually dragging.
        if self._mode is not None:
            painter.setPen(QPen(QColor(255, 255, 255, 70), 1))
            for third in (1 / 3, 2 / 3):
                x = rect.left() + rect.width() * third
                y = rect.top() + rect.height() * third
                painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
                painter.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))

        # Corner handles, so the affordance is visible before it is hovered.
        painter.setBrush(QColor(255, 255, 255, 235))
        painter.setPen(Qt.PenStyle.NoPen)
        handle = 3.5
        for corner in (rect.topLeft(), rect.topRight(),
                       rect.bottomLeft(), rect.bottomRight()):
            painter.drawEllipse(corner, handle, handle)


def _resize(origin: FreeCrop, zone: str, dx: float, dy: float) -> FreeCrop:
    """Move the grabbed edges by (dx, dy), keeping the rectangle sane."""
    moves_left, moves_top, moves_right, moves_bottom = _ZONES[zone]
    minimum = FreeCrop.MIN_FRACTION

    left = origin.left
    top = origin.top
    right = origin.left + origin.width
    bottom = origin.top + origin.height

    if moves_left:
        left = min(right - minimum, max(0.0, left + dx))
    if moves_right:
        right = max(left + minimum, min(1.0, right + dx))
    if moves_top:
        top = min(bottom - minimum, max(0.0, top + dy))
    if moves_bottom:
        bottom = max(top + minimum, min(1.0, bottom + dy))

    return FreeCrop(left, top, right - left, bottom - top).normalised()
