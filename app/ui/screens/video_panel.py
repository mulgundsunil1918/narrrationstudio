"""The video card on the Export screen: put the narration back on the picture.

Subtitle styling is shown, never described. Every control redraws a real frame
from the user's own video with their own words on it, because nobody can judge
"outline width 0.14" from a number, and a style that looked fine as a setting
and wrong on the video is a wasted export.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from app.ui.state import AppState
from app.ui.widgets.common import (
    Card,
    GhostButton,
    LabeledSlider,
    PrimaryButton,
    Segmented,
    caption,
    label,
    muted,
    section_label,
)
from app.video.style import PRESETS, SubtitleStyle

logger = logging.getLogger(__name__)

#: Colours worth offering. Anything else is a colour picker away.
COLOURS: tuple[tuple[str, str], ...] = (
    ("White", "#FFFFFF"),
    ("Yellow", "#FFD54A"),
    ("Black", "#111111"),
    ("Sky", "#7FD1FF"),
)

PREVIEW_WIDTH = 420


class VideoPanel(Card):
    """Choose the video, style the subtitles, and export."""

    export_requested = Signal(object)   # VideoExportRequest

    def __init__(self, state: AppState, parent: QWidget | None = None) -> None:
        super().__init__(parent=parent)
        self._state = state
        self._style = SubtitleStyle()
        self._video: Path | None = state.media_path

        self.add(section_label("Video"))
        self.add(
            muted(
                "Put the narration onto your video. The picture is copied across "
                "untouched unless you ask for the subtitles to be burned in.",
                wrap=True,
            )
        )

        self.add(self._build_source())
        self.add(self._build_subtitle_mode())
        self._style_panel = self._build_style()
        self.add(self._style_panel)
        self.add(self._build_preview())
        self.add_layout(self._build_actions())

        state.project_changed.connect(self._on_project_changed)
        self._refresh()

    # -- source ----------------------------------------------------------

    def _build_source(self) -> QWidget:
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 6, 0, 0)
        row.setSpacing(10)

        self._source_label = muted("")
        self._source_label.setWordWrap(True)
        row.addWidget(self._source_label, 1)

        choose = GhostButton("Choose Video…")
        choose.clicked.connect(self.choose_video)
        row.addWidget(choose)
        return holder

    def choose_video(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose the video", str(Path.home()),
            "Video (*.mp4 *.mov *.m4v *.mkv *.webm);;All files (*)",
        )
        if path:
            self._video = Path(path)
            self._state.media_path = self._video
            self._refresh()

    # -- subtitles -------------------------------------------------------

    def _build_subtitle_mode(self) -> QWidget:
        holder = QWidget()
        column = QVBoxLayout(holder)
        column.setContentsMargins(0, 12, 0, 0)
        column.setSpacing(7)
        column.addWidget(label("Subtitles", "Muted"))

        self._mode = Segmented(
            [
                ("none", "No subtitles"),
                ("sidecar", "Separate file"),
                ("burn", "Burn into picture"),
            ],
            initial="none",
        )
        self._mode.changed.connect(self._on_mode)
        column.addWidget(self._mode)

        self._mode_note = caption("", wrap=True)
        column.addWidget(self._mode_note)
        return holder

    def _on_mode(self, key: str) -> None:
        self._style_panel.setVisible(key == "burn")
        self._preview_holder.setVisible(key == "burn")
        self._describe_mode()
        if key == "burn":
            self._redraw_preview()

    def _describe_mode(self) -> None:
        notes = {
            "none": "Just the narration on your video. Nothing is re-encoded, so "
                    "this is quick and the picture is untouched.",
            "sidecar": "Saves a .srt next to the video. Players pick it up "
                       "automatically, and the viewer can switch it off. The "
                       "picture is still untouched.",
            "burn": "Paints the words into the picture, so they show anywhere — "
                    "including on WhatsApp and social media, where separate "
                    "subtitle files are ignored. The video is re-encoded, which "
                    "takes longer and cannot be undone afterwards.",
        }
        self._mode_note.setText(notes.get(self._mode.current(), ""))

    # -- styling ---------------------------------------------------------

    def _build_style(self) -> QWidget:
        holder = QWidget()
        column = QVBoxLayout(holder)
        column.setContentsMargins(0, 12, 0, 0)
        column.setSpacing(9)

        column.addWidget(label("Look", "Muted"))
        self._preset = Segmented(
            [(name, name) for name, _description, _style in PRESETS], initial="Clean"
        )
        self._preset.changed.connect(self._on_preset)
        column.addWidget(self._preset)

        fonts = QHBoxLayout()
        fonts.setSpacing(10)
        self._font = QComboBox()
        self._font.addItems(_font_choices())
        self._font.setCurrentText(self._style.font_family)
        self._font.currentTextChanged.connect(
            lambda value: self._change(font_family=value)
        )
        fonts.addWidget(self._font, 2)

        self._colour = QComboBox()
        for name, value in COLOURS:
            self._colour.addItem(name, value)
        self._colour.currentIndexChanged.connect(self._on_colour)
        fonts.addWidget(self._colour, 1)
        column.addLayout(fonts)

        # Tenths of a percent, so the slider is smooth at the sizes that matter.
        self._size = LabeledSlider(
            "Text size", 25, 90, int(self._style.size_percent * 10),
            suffix="% of height", scale=0.1, decimals=1,
        )
        self._size.valueChanged.connect(
            lambda value: self._change(size_percent=value / 10)
        )
        column.addWidget(self._size)

        self._margin = LabeledSlider(
            "Distance from the edge", 0, 25, int(self._style.margin_percent),
            suffix="% of height",
        )
        self._margin.valueChanged.connect(
            lambda value: self._change(margin_percent=float(value))
        )
        column.addWidget(self._margin)

        column.addWidget(label("Position", "Muted"))
        self._position = Segmented(
            [("bottom", "Bottom"), ("middle", "Middle"), ("top", "Top")],
            initial="bottom",
        )
        self._position.changed.connect(
            lambda value: self._change(position=value)
        )
        column.addWidget(self._position)

        self._alignment = Segmented(
            [("left", "Left"), ("center", "Centre"), ("right", "Right")],
            initial="center",
        )
        self._alignment.changed.connect(
            lambda value: self._change(alignment=value)
        )
        column.addWidget(self._alignment)

        toggles = QHBoxLayout()
        toggles.setSpacing(16)
        self._outline = QCheckBox("Outline")
        self._outline.setChecked(self._style.outline)
        self._outline.setToolTip("Keeps light text readable over a light picture")
        self._outline.toggled.connect(lambda on: self._change(outline=on))
        toggles.addWidget(self._outline)

        self._box = QCheckBox("Background band")
        self._box.setChecked(self._style.box)
        self._box.setToolTip("A solid strip behind the words, for busy footage")
        self._box.toggled.connect(lambda on: self._change(box=on))
        toggles.addWidget(self._box)

        self._bold = QCheckBox("Bold")
        self._bold.setChecked(self._style.bold)
        self._bold.toggled.connect(lambda on: self._change(bold=on))
        toggles.addWidget(self._bold)
        toggles.addStretch(1)
        column.addLayout(toggles)

        holder.setVisible(False)
        return holder

    def _on_preset(self, name: str) -> None:
        for label_text, _description, style in PRESETS:
            if label_text == name:
                self._style = style
                break
        self._sync_controls()
        self._redraw_preview()

    def _on_colour(self, index: int) -> None:
        self._change(colour=self._colour.itemData(index))

    def _change(self, **values) -> None:
        self._style = self._style.with_changes(**values)
        self._redraw_preview()

    def _sync_controls(self) -> None:
        """Push the current style back into the controls without echoing changes."""
        for widget in (self._font, self._colour, self._size, self._margin,
                       self._position, self._alignment, self._outline,
                       self._box, self._bold):
            widget.blockSignals(True)
        try:
            self._font.setCurrentText(self._style.font_family)
            index = self._colour.findData(self._style.colour)
            self._colour.setCurrentIndex(index if index >= 0 else 0)
            self._size.setValue(int(self._style.size_percent * 10))
            self._margin.setValue(int(self._style.margin_percent))
            self._position.select(self._style.position, emit=False)
            self._alignment.select(self._style.alignment, emit=False)
            self._outline.setChecked(self._style.outline)
            self._box.setChecked(self._style.box)
            self._bold.setChecked(self._style.bold)
        finally:
            for widget in (self._font, self._colour, self._size, self._margin,
                           self._position, self._alignment, self._outline,
                           self._box, self._bold):
                widget.blockSignals(False)

    # -- preview ---------------------------------------------------------

    def _build_preview(self) -> QWidget:
        self._preview_holder = QWidget()
        column = QVBoxLayout(self._preview_holder)
        column.setContentsMargins(0, 12, 0, 0)
        column.setSpacing(6)
        column.addWidget(label("Preview", "Muted"))

        self._preview = QLabel()
        self._preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview.setMinimumHeight(150)
        column.addWidget(self._preview)

        self._preview_note = caption("", wrap=True)
        column.addWidget(self._preview_note)
        self._preview_holder.setVisible(False)
        return self._preview_holder

    def _redraw_preview(self) -> None:
        """Draw a real frame of the user's video with their own caption on it.

        Keyed off the chosen mode, not the widget's visibility: a widget inside
        a parent that has not been shown yet reports itself invisible, and the
        preview would then stay blank until something else happened to redraw it.
        """
        if self._mode.current() != "burn":
            return
        from app.video.preview import preview_frame

        text = next(
            (s.text for s in self._state.segments if s.text.strip()),
            "Your subtitles will look like this.",
        )
        try:
            image, note = preview_frame(self._video, text, self._style, PREVIEW_WIDTH)
        except Exception as exc:
            logger.warning("Preview failed: %s", exc)
            self._preview.setText("The preview could not be drawn.")
            self._preview_note.setText("The export itself is unaffected.")
            return

        self._preview.setPixmap(QPixmap.fromImage(image))
        self._preview_note.setText(note)

    # -- actions ---------------------------------------------------------

    def _build_actions(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setContentsMargins(0, 14, 0, 0)
        row.setSpacing(9)
        self._status = caption("")
        row.addWidget(self._status, 1)

        self._export = PrimaryButton("Export Video")
        self._export.clicked.connect(self._on_export)
        row.addWidget(self._export)
        return row

    def _on_export(self) -> None:
        from app.video.export import VideoExportRequest

        if self._video is None:
            self._state.report("Choose a video first.", "warning")
            return

        suffix = self._video.suffix or ".mp4"
        default = self._video.with_name(f"{self._video.stem}_Narrated{suffix}")
        path, _ = QFileDialog.getSaveFileName(
            self, "Save the video", str(default),
            f"Video (*{suffix});;MP4 (*.mp4);;QuickTime (*.mov)",
        )
        if not path:
            return

        mode = self._mode.current()
        self.export_requested.emit(
            VideoExportRequest(
                video_path=self._video,
                output_path=Path(path),
                audio_path=self._state.generated_path,
                segments=list(self._state.segments),
                sidecar_subtitles=mode == "sidecar",
                burn_subtitles=mode == "burn",
                style=self._style,
            )
        )

    # -- state -----------------------------------------------------------

    def _on_project_changed(self) -> None:
        if self._video is None and self._state.media_path is not None:
            self._video = self._state.media_path
        self._refresh()

    def _refresh(self) -> None:
        if self._video is None:
            self._source_label.setText(
                "No video chosen. Pick the video you want the narration on."
            )
        else:
            self._source_label.setText(f"Using {self._video.name}")

        ready = self._video is not None and self._state.generated_audio is not None
        self._export.setEnabled(ready)
        if self._video is None:
            self._status.setText("")
        elif self._state.generated_audio is None:
            self._status.setText("Generate the narration first.")
        else:
            self._status.setText("")
        self._describe_mode()

    def set_busy(self, busy: bool, message: str = "") -> None:
        self._export.setEnabled(not busy and self._video is not None)
        self._export.setText("Exporting…" if busy else "Export Video")
        if message:
            self._status.setText(message)


def _font_choices() -> list[str]:
    """Fonts that exist on this Mac, with the readable ones first."""
    from PySide6.QtGui import QFontDatabase

    available = set(QFontDatabase.families())
    preferred = [
        "Helvetica Neue", "Helvetica", "Arial", "Avenir Next", "SF Pro Text",
        "Verdana", "Georgia", "Futura", "Gill Sans", "Trebuchet MS",
    ]
    ordered = [name for name in preferred if name in available]
    ordered += sorted(name for name in available
                      if name not in ordered and not name.startswith("."))
    return ordered[:60] or ["Helvetica"]
