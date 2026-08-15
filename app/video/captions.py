"""Draw a caption into a small image that can be laid over a video frame.

The FFmpeg libraries bundled with the app are built without libass and without
freetype, so the usual ``subtitles=`` filter does not exist here. Rather than
send everyone back to installing FFmpeg — the thing this app just stopped
doing — the text is drawn with Qt, which is already present, already knows the
fonts on this Mac, and renders them better than libass does anyway.

Each caption becomes one small RGBA block plus the position it belongs at, not
a full-frame overlay: a 1080p frame is eight megabytes, and a hundred captions
held as full frames would be most of a gigabyte for no reason.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
from PySide6.QtCore import QRect, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
)

from app.video.style import SubtitleStyle

logger = logging.getLogger(__name__)

#: Room for the outline to sit in without being clipped at the edges.
PADDING = 8


@dataclass(frozen=True)
class CaptionLayer:
    """A caption drawn once, ready to blend onto any frame it belongs on."""

    rgb: np.ndarray      # (h, w, 3) uint8
    alpha: np.ndarray    # (h, w, 1) float32 in 0..1
    x: int
    y: int

    @property
    def height(self) -> int:
        return self.rgb.shape[0]

    @property
    def width(self) -> int:
        return self.rgb.shape[1]


def _alignment_flag(alignment: str) -> Qt.AlignmentFlag:
    return {
        "left": Qt.AlignmentFlag.AlignLeft,
        "center": Qt.AlignmentFlag.AlignHCenter,
        "right": Qt.AlignmentFlag.AlignRight,
    }.get(alignment, Qt.AlignmentFlag.AlignHCenter)


def build_font(style: SubtitleStyle, font_size: int) -> QFont:
    font = QFont(style.font_family)
    font.setPixelSize(font_size)
    font.setBold(style.bold)
    # Hinting off keeps letter spacing even at large sizes, which matters more
    # on video than the crispness it trades away.
    font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    return font


def render_caption(
    text: str, style: SubtitleStyle, width: int, height: int
) -> CaptionLayer | None:
    """Draw ``text`` for a ``width`` x ``height`` video. None if there is nothing."""
    text = " ".join(text.split())
    if not text:
        return None

    metrics = style.scaled_to(height)
    font = build_font(style, metrics.font_size)
    measure = QFontMetrics(font)

    wrap_width = max(50, round(width * style.max_width_percent / 100))
    flags = int(_alignment_flag(style.alignment) | Qt.TextFlag.TextWordWrap)
    bounds = measure.boundingRect(QRect(0, 0, wrap_width, height), flags, text)

    line_gap = round(measure.height() * (style.line_spacing - 1.0))
    lines = max(1, bounds.height() // max(1, measure.height()))
    block_height = bounds.height() + line_gap * (lines - 1)

    pad = PADDING + metrics.outline_px
    box_pad_x = round(metrics.font_size * 0.45) if style.box else 0
    box_pad_y = round(metrics.font_size * 0.25) if style.box else 0

    image_width = min(width, bounds.width() + (pad + box_pad_x) * 2)
    image_height = min(height, block_height + (pad + box_pad_y) * 2)

    image = QImage(image_width, image_height, QImage.Format.Format_RGBA8888)
    image.fill(Qt.GlobalColor.transparent)

    painter = QPainter(image)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        painter.setFont(font)

        if style.box:
            colour = QColor(style.box_colour)
            colour.setAlphaF(max(0.0, min(1.0, style.box_opacity)))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(colour)
            radius = metrics.font_size * 0.18
            painter.drawRoundedRect(
                QRectF(0, 0, image_width, image_height), radius, radius
            )

        text_area = QRect(
            pad + box_pad_x, pad + box_pad_y,
            image_width - (pad + box_pad_x) * 2,
            image_height - (pad + box_pad_y) * 2,
        )

        if metrics.outline_px:
            # An outline has to be a stroked path. Drawing the text repeatedly at
            # small offsets — the usual shortcut — leaves visible corners on
            # anything thicker than a pixel or two.
            path = QPainterPath()
            _add_wrapped_text(path, text, font, text_area, style, measure, line_gap)
            pen = QPen(QColor(style.outline_colour))
            pen.setWidthF(metrics.outline_px * 2)      # stroke straddles the edge
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(style.colour))
            painter.drawPath(path)
        else:
            painter.setPen(QColor(style.colour))
            painter.drawText(text_area, flags, text)
    finally:
        painter.end()

    return _to_layer(image, style, width, height, metrics)


def _add_wrapped_text(
    path: QPainterPath,
    text: str,
    font: QFont,
    area: QRect,
    style: SubtitleStyle,
    measure: QFontMetrics,
    line_gap: int,
) -> None:
    """Lay the text out line by line and add each to ``path``."""
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if current and measure.horizontalAdvance(candidate) > area.width():
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)

    step = measure.height() + line_gap
    y = area.top() + measure.ascent()
    for line in lines:
        advance = measure.horizontalAdvance(line)
        if style.alignment == "left":
            x = area.left()
        elif style.alignment == "right":
            x = area.right() - advance
        else:
            x = area.left() + (area.width() - advance) / 2
        path.addText(float(x), float(y), font, line)
        y += step


def _to_layer(
    image: QImage, style: SubtitleStyle, width: int, height: int, metrics
) -> CaptionLayer:
    """Convert the drawn image to arrays and work out where it sits on the frame."""
    image = image.convertToFormat(QImage.Format.Format_RGBA8888)
    w, h = image.width(), image.height()
    buffer = image.constBits()
    # bytesPerLine can exceed width*4 for alignment, so go through the stride.
    stride = image.bytesPerLine()
    raw = np.frombuffer(bytes(buffer)[: stride * h], dtype=np.uint8).reshape(h, stride)
    pixels = raw[:, : w * 4].reshape(h, w, 4)

    if style.alignment == "left":
        x = metrics.margin
    elif style.alignment == "right":
        x = width - w - metrics.margin
    else:
        x = (width - w) // 2

    if style.position == "top":
        y = metrics.margin
    elif style.position == "middle":
        y = (height - h) // 2
    else:
        y = height - h - metrics.margin

    return CaptionLayer(
        rgb=np.ascontiguousarray(pixels[:, :, :3]),
        alpha=np.ascontiguousarray(pixels[:, :, 3:4].astype(np.float32) / 255.0),
        x=max(0, min(width - w, x)),
        y=max(0, min(height - h, y)),
    )


def blend(frame: np.ndarray, layer: CaptionLayer) -> np.ndarray:
    """Alpha-composite one caption onto an RGB frame, in place."""
    top, left = layer.y, layer.x
    bottom, right = top + layer.height, left + layer.width
    if bottom > frame.shape[0] or right > frame.shape[1]:
        return frame

    region = frame[top:bottom, left:right].astype(np.float32)
    blended = region * (1.0 - layer.alpha) + layer.rgb.astype(np.float32) * layer.alpha
    frame[top:bottom, left:right] = blended.astype(np.uint8)
    return frame
