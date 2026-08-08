"""Draw the application icon and render it to .icns.

Generated rather than shipped as a binary so it stays in version control as
readable code, and so the accent colour can follow the app's palette.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QGuiApplication, QLinearGradient, QPainter, QPixmap

# macOS asks for these sizes; iconutil builds the .icns from the set.
SIZES = (16, 32, 64, 128, 256, 512, 1024)

BACKDROP_TOP = "#4C8DFF"
BACKDROP_BOTTOM = "#2F62E8"
MARK = "#FFFFFF"


def draw(size: int) -> QPixmap:
    """A speech waveform on a rounded tile — narration, at a glance."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    # macOS icons sit inside a margin rather than filling the square.
    margin = size * 0.09
    tile = QRectF(margin, margin, size - margin * 2, size - margin * 2)

    gradient = QLinearGradient(tile.topLeft(), tile.bottomRight())
    gradient.setColorAt(0.0, QColor(BACKDROP_TOP))
    gradient.setColorAt(1.0, QColor(BACKDROP_BOTTOM))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(gradient))
    painter.drawRoundedRect(tile, tile.width() * 0.225, tile.height() * 0.225)

    # Waveform bars, tallest in the middle.
    heights = (0.24, 0.44, 0.70, 0.94, 0.62, 0.86, 0.40, 0.22)
    bar_area = tile.adjusted(
        tile.width() * 0.17, tile.height() * 0.17,
        -tile.width() * 0.17, -tile.height() * 0.17,
    )
    slot = bar_area.width() / len(heights)
    bar_width = slot * 0.46
    radius = bar_width / 2
    painter.setBrush(QColor(MARK))

    for index, fraction in enumerate(heights):
        height = bar_area.height() * fraction
        centre_x = bar_area.left() + slot * (index + 0.5)
        rect = QRectF(
            centre_x - bar_width / 2,
            bar_area.center().y() - height / 2,
            bar_width,
            height,
        )
        painter.drawRoundedRect(rect, radius, radius)

    painter.end()
    return pixmap


def build(destination: Path) -> Path:
    """Write <destination>/AppIcon.icns, falling back to a PNG without Xcode."""
    destination.mkdir(parents=True, exist_ok=True)
    iconset = destination / "AppIcon.iconset"
    if iconset.exists():
        shutil.rmtree(iconset)
    iconset.mkdir()

    for size in SIZES:
        draw(size).save(str(iconset / f"icon_{size}x{size}.png"))
        # Retina variants: a 32px @2x file must contain a 64px image.
        if size * 2 <= 1024:
            draw(size * 2).save(str(iconset / f"icon_{size}x{size}@2x.png"))

    icns = destination / "AppIcon.icns"
    if shutil.which("iconutil"):
        subprocess.run(
            ["iconutil", "-c", "icns", str(iconset), "-o", str(icns)], check=True
        )
        shutil.rmtree(iconset)
        return icns

    # No Xcode command line tools: keep the largest PNG so the build still works.
    fallback = destination / "AppIcon.png"
    draw(1024).save(str(fallback))
    return fallback


def main() -> int:
    # An offscreen QGuiApplication is enough for QPainter.
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QGuiApplication([])
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "build"
    path = build(target)
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
