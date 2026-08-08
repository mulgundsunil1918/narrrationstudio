"""Drag-and-drop import target.

Accepts subtitles, video and audio, decides what a dropped file is by extension,
and refuses anything else with a reason rather than ignoring the drop.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QDragEnterEvent, QDropEvent, QPainter, QPen
from PySide6.QtWidgets import QFileDialog, QVBoxLayout, QWidget

from app.config import PROJECT_SUFFIX
from app.ui.theme import palette
from app.ui.widgets.common import PrimaryButton, caption, label, muted

SUBTITLE_SUFFIXES = {".srt", ".txt", ".md", ".markdown"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".mkv", ".avi"}
AUDIO_SUFFIXES = {".wav", ".mp3", ".m4a", ".aac", ".flac"}
ACCEPTED = SUBTITLE_SUFFIXES | VIDEO_SUFFIXES | AUDIO_SUFFIXES
PROJECT_SUFFIXES = {PROJECT_SUFFIX, ".pediavid"}  # older files still open


def classify(path: Path) -> str:
    """Return "subtitles", "video", "audio", "project" or "unsupported"."""
    suffix = path.suffix.lower()
    if suffix in SUBTITLE_SUFFIXES:
        return "subtitles"
    if suffix in VIDEO_SUFFIXES:
        return "video"
    if suffix in AUDIO_SUFFIXES:
        return "audio"
    if suffix in PROJECT_SUFFIXES:
        return "project"
    return "unsupported"


def file_filter() -> str:
    subtitles = " ".join(f"*{s}" for s in sorted(SUBTITLE_SUFFIXES))
    video = " ".join(f"*{s}" for s in sorted(VIDEO_SUFFIXES))
    audio = " ".join(f"*{s}" for s in sorted(AUDIO_SUFFIXES))
    return (
        f"All supported ({subtitles} {video} {audio});;"
        f"Subtitles ({subtitles});;Video ({video});;Audio ({audio})"
    )


class DropZone(QWidget):
    """A large dashed target that also opens a file dialog when clicked."""

    #: (path, kind) for a file the zone accepted.
    file_dropped = Signal(Path, str)
    #: (path, reason) for a file it refused, so the caller can explain.
    file_rejected = Signal(Path, str)

    def __init__(
        self,
        headline: str = "Drop a video or subtitle file here",
        sub: str = "SRT · MP4 · MOV · WAV · MP3",
        button_text: str = "Choose File…",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setMinimumHeight(230)
        self._hovering = False

        column = QVBoxLayout(self)
        column.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column.setSpacing(9)

        glyph = label("↓")
        glyph.setAlignment(Qt.AlignmentFlag.AlignCenter)
        glyph.setStyleSheet(f"font-size: 30px; color: {palette().text_faint};")
        column.addWidget(glyph)

        top = label(headline, "Heading")
        top.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column.addWidget(top)

        formats = caption(sub)
        formats.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column.addWidget(formats)

        column.addSpacing(8)
        self.browse = PrimaryButton(button_text)
        self.browse.setFixedWidth(190)
        self.browse.clicked.connect(self.open_dialog)
        column.addWidget(self.browse, alignment=Qt.AlignmentFlag.AlignCenter)

    # -- painting --------------------------------------------------------

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        current = palette()

        pen = QPen(QColor(current.accent if self._hovering else current.border_strong))
        pen.setWidth(2 if self._hovering else 1)
        pen.setStyle(Qt.PenStyle.DashLine)
        pen.setDashPattern([6, 5])
        painter.setPen(pen)
        if self._hovering:
            painter.setBrush(QColor(current.accent_soft))
        else:
            painter.setBrush(QColor(current.surface_alt))
        painter.drawRoundedRect(self.rect().adjusted(1, 1, -2, -2), 14, 14)

    # -- drag and drop ---------------------------------------------------

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self._hovering = True
            self.update()

    def dragLeaveEvent(self, _event) -> None:
        self._hovering = False
        self.update()

    def dropEvent(self, event: QDropEvent) -> None:
        self._hovering = False
        self.update()

        urls = event.mimeData().urls()
        if not urls:
            return
        path = Path(urls[0].toLocalFile())
        event.acceptProposedAction()
        self.handle(path)

    def handle(self, path: Path) -> None:
        """Validate a path and emit the right signal. Never silent."""
        if not path.exists():
            self.file_rejected.emit(
                path, "That file could not be found. It may have been moved."
            )
            return
        if path.is_dir():
            self.file_rejected.emit(
                path, "That is a folder. Drop a single subtitle, video or audio file."
            )
            return

        kind = classify(path)
        if kind == "unsupported":
            listed = ", ".join(sorted(s.lstrip(".").upper() for s in ACCEPTED))
            self.file_rejected.emit(
                path,
                f"“{path.suffix or 'This file type'}” is not supported. "
                f"Use one of: {listed}.",
            )
            return
        self.file_dropped.emit(path, kind)

    def open_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose a file", str(Path.home()), file_filter()
        )
        if path:
            self.handle(Path(path))
