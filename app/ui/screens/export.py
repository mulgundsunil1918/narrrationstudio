"""Export: choose a destination and write the files.

Every export reports a visible state — writing, written, or failed with a
reason. Codec detail stays in Advanced.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QLineEdit,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.core.status import ErrorCode, OperationError, OperationState
from app.pipeline import OUTPUT_SUFFIX
from app.ui.state import AppState
from app.ui.theme import mono_font, palette
from app.utils.platform import file_manager_name
from app.ui.widgets.common import (
    Card,
    Divider,
    GhostButton,
    Pill,
    PrimaryButton,
    SecondaryButton,
    Spinner,
    caption,
    clear_layout,
    clock,
    heading,
    label,
    muted,
    section_label,
    title,
)
from app.ui.workers import ExportWorker, run_in_thread


class ExportScreen(QWidget):
    """WAV, MP3 and SRT export."""

    def __init__(self, state: AppState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._state = state
        self._thread = None
        self._video_thread = None
        self._video_worker = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build_header())

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # Pinned on: a scrollbar that comes and goes changes the available width,
        # which re-wraps the content, which toggles the scrollbar again.
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        holder = QWidget()
        holder.setObjectName("Workspace")
        self._column = QVBoxLayout(holder)
        self._column.setContentsMargins(28, 22, 28, 32)
        self._column.setSpacing(18)
        self._column.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._column.addWidget(self._build_destination())
        self._column.addWidget(self._build_audio())
        self._column.addWidget(self._build_subtitles())
        self._column.addWidget(self._build_video())
        scroll.setWidget(holder)
        outer.addWidget(scroll, 1)

        state.generation_finished.connect(lambda _o: self.refresh())
        state.project_changed.connect(self.refresh)
        self.refresh()

    # -- construction ----------------------------------------------------

    def _build_header(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("TopBar")
        row = QHBoxLayout(bar)
        row.setContentsMargins(28, 16, 28, 16)
        column = QVBoxLayout()
        column.setSpacing(2)
        column.addWidget(title("Export"))
        column.addWidget(muted("Save your narration."))
        row.addLayout(column)
        row.addStretch(1)
        self._status = Pill("Nothing to export yet", "neutral")
        row.addWidget(self._status)
        return bar

    def _build_destination(self) -> QWidget:
        card = Card()
        card.add(section_label("Where it will be saved"))

        card.add(label("File name", "Muted"))
        self._filename = QLineEdit()
        self._filename.setPlaceholderText("Name your file, e.g. MyVideo_Narration")
        self._filename.textChanged.connect(self._update_preview)
        card.add(self._filename)

        card.add(label("Folder", "Muted"))
        row = QHBoxLayout()
        row.setSpacing(9)
        self._folder = QLineEdit(str(Path.home() / "Desktop"))
        self._folder.setReadOnly(True)
        row.addWidget(self._folder, 1)
        browse = SecondaryButton("Choose…")
        browse.clicked.connect(self._choose_folder)
        row.addWidget(browse)
        card.add_layout(row)

        card.add(Divider())
        card.add(section_label("Your file will be saved as"))
        self._preview = QLineEdit()
        self._preview.setReadOnly(True)
        self._preview.setFont(mono_font(11))
        card.add(self._preview)
        self._preview_extra = QLineEdit()
        self._preview_extra.setReadOnly(True)
        self._preview_extra.setFont(mono_font(11))
        self._preview_extra.setVisible(False)
        card.add(self._preview_extra)
        card.add(caption("The extension is added for you.", wrap=True))
        return card

    def _update_preview(self) -> None:
        """Always show the exact path, so nobody has to guess where it went."""
        wav = self._target(".wav")
        self._preview.setText(str(wav))
        self._preview.setToolTip(str(wav))
        wants_mp3 = self._mp3_check.isChecked()
        if wants_mp3:
            mp3 = self._target(".mp3")
            self._preview_extra.setText(str(mp3))
            self._preview_extra.setToolTip(str(mp3))
        self._preview_extra.setVisible(wants_mp3)

    def _build_audio(self) -> QWidget:
        card = Card()
        card.add(section_label("Audio"))

        self._wav_row = self._format_row(
            "WAV", "24 kHz · 16-bit PCM · mono — the format to bring into a video editor"
        )
        card.add(self._wav_row)
        self._mp3_check = QCheckBox("Also write an MP3 (192 kbps)")
        self._mp3_check.stateChanged.connect(lambda _s: self._update_preview())
        card.add(self._mp3_check)

        card.add(Divider())
        row = QHBoxLayout()
        row.setSpacing(10)
        self._spinner = Spinner(18)
        self._spinner.setVisible(False)
        row.addWidget(self._spinner)
        self._progress_note = muted("")
        row.addWidget(self._progress_note)
        row.addStretch(1)
        self._export_button = PrimaryButton("Export Audio")
        self._export_button.clicked.connect(self.export_audio)
        row.addWidget(self._export_button)
        card.add_layout(row)

        self._result_row = QHBoxLayout()
        self._result_row.setSpacing(9)
        card.add_layout(self._result_row)
        return card

    def _build_subtitles(self) -> QWidget:
        card = Card()
        card.add(section_label("Subtitles"))
        card.add(
            muted(
                "Export your edited captions. Timestamps are written exactly as "
                "they are in your project — editing text never moves them.",
                wrap=True,
            )
        )
        row = QHBoxLayout()
        row.addStretch(1)
        self._srt_button = SecondaryButton("Export SRT")
        self._srt_button.clicked.connect(self.export_srt)
        row.addWidget(self._srt_button)
        card.add_layout(row)
        return card

    def _build_video(self) -> QWidget:
        from app.ui.screens.video_panel import VideoPanel

        self._video_panel = VideoPanel(self._state)
        self._video_panel.export_requested.connect(self.export_video)
        return self._video_panel

    # -- video -----------------------------------------------------------

    def export_video(self, request) -> None:
        """Run the export off the UI thread, so a long re-encode cannot freeze it."""
        from app.ui.workers import VideoExportWorker, run_in_thread

        if self._video_thread is not None:
            self._state.report("A video export is already running.", "warning")
            return

        worker = VideoExportWorker(request)
        # Bound methods with an explicit queued connection: a lambda here has no
        # thread affinity and would touch widgets from the worker thread.
        worker.progress.connect(self._on_video_progress, Qt.ConnectionType.QueuedConnection)
        worker.finished.connect(self._on_video_finished, Qt.ConnectionType.QueuedConnection)
        worker.failed.connect(self._on_video_failed, Qt.ConnectionType.QueuedConnection)
        worker.cancelled.connect(self._on_video_cancelled, Qt.ConnectionType.QueuedConnection)
        self._video_worker = worker
        self._video_thread = run_in_thread(worker, self)
        self._video_panel.set_busy(True, "Starting…")

    def _on_video_progress(self, fraction, message: str) -> None:
        if fraction is None:
            self._video_panel.set_busy(True, message)
        else:
            self._video_panel.set_busy(True, f"{message}  ({int(fraction * 100)}%)")

    def _on_video_finished(self, result) -> None:
        self._video_thread = None
        self._video_worker = None
        self._video_panel.set_busy(False, "")
        extra = ""
        if result.subtitle_path is not None:
            extra = f" · {result.subtitle_path.name} saved beside it"
        elif result.burned_captions:
            extra = f" · {result.burned_captions} subtitles burned in"
        self._state.report(f"Saved {result.path.name}{extra}", "success")
        self._show_result(result.path)
        for warning in result.warnings:
            self._state.report(warning, "warning")

    def _on_video_failed(self, error) -> None:
        self._video_thread = None
        self._video_worker = None
        self._video_panel.set_busy(False, "")
        self._state.error_raised.emit(error)

    def _on_video_cancelled(self) -> None:
        self._video_thread = None
        self._video_worker = None
        self._video_panel.set_busy(False, "Stopped.")

    def _format_row(self, name: str, detail: str) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        layout.addWidget(label(name, "Heading"))
        layout.addWidget(muted(detail), 1)
        return row

    # -- state -----------------------------------------------------------

    def refresh(self) -> None:
        ready = self._state.outcome is not None
        self._export_button.setEnabled(ready)
        self._srt_button.setEnabled(self._state.has_captions)

        if not self._filename.text():
            name = self._state.project.name or "Narration"
            self._filename.setText(f"{name.replace(' ', '_')}{OUTPUT_SUFFIX}")
        self._update_preview()

        if ready and self._state.outcome is not None:
            self._status.set_status(
                f"Ready · {clock(self._state.outcome.duration_ms)}", "success"
            )
        elif self._state.has_captions:
            self._status.set_status("Generate the narration first", "neutral")
        else:
            self._status.set_status("Nothing to export yet", "neutral")

    def _choose_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Choose a destination folder", self._folder.text()
        )
        if folder:
            self._folder.setText(folder)

    def _target(self, suffix: str) -> Path:
        name = self._filename.text().strip() or "Narration"
        return Path(self._folder.text()) / f"{name}{suffix}"

    # -- actions ---------------------------------------------------------

    def export_audio(self) -> None:
        outcome = self._state.outcome
        if outcome is None:
            self._state.report("Generate the narration before exporting.", "warning")
            return

        target = self._target(".wav")
        folder = target.parent
        if not folder.exists():
            try:
                folder.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                self._state.raise_error(
                    OperationError(
                        ErrorCode.OUTPUT_NOT_WRITABLE,
                        "That destination folder could not be created.",
                        reason=str(exc),
                        recommended_action="Choose a different folder.",
                        operation="export",
                    )
                )
                return

        self._clear_result()
        self._spinner.setVisible(True)
        self._spinner.start()
        self._progress_note.setText(f"Writing {target.name}…")
        self._export_button.setEnabled(False)
        self._status.set_status("Exporting…", "info")
        self._state.set_state(OperationState.PROCESSING)

        worker = ExportWorker(
            outcome.audio, outcome.sample_rate, target, self._mp3_check.isChecked()
        )
        worker.finished.connect(self._on_exported, Qt.ConnectionType.QueuedConnection)
        worker.failed.connect(self._on_export_failed, Qt.ConnectionType.QueuedConnection)
        self._thread = run_in_thread(worker, self)

    def _on_exported(self, paths: list[Path]) -> None:
        self._spinner.stop()
        self._spinner.setVisible(False)
        self._export_button.setEnabled(True)
        names = ", ".join(p.name for p in paths)
        self._progress_note.setText(f"Saved to {paths[0].parent}" if paths else "Saved")
        self._status.set_status("Exported ✓", "success")
        self._state.set_state(OperationState.COMPLETED)
        self._state.generated_path = paths[0] if paths else None
        self._state.report(f"Exported {names}", "success")
        self._show_result(paths[0])

    def _on_export_failed(self, error: OperationError) -> None:
        self._spinner.stop()
        self._spinner.setVisible(False)
        self._export_button.setEnabled(True)
        self._progress_note.setText("")
        self._status.set_status("Export failed", "error")
        self._state.raise_error(error)

    def _clear_result(self) -> None:
        clear_layout(self._result_row)

    def _show_result(self, path: Path) -> None:
        self._clear_result()
        saved = label(f"✓  {path.name}", "Body")
        saved.setToolTip(str(path))
        self._result_row.addWidget(saved)
        reveal = SecondaryButton(f"Show in {file_manager_name()}")
        reveal.clicked.connect(lambda: self._reveal(path))
        self._result_row.addWidget(reveal)
        self._result_row.addStretch(1)

    def _reveal(self, path: Path) -> None:
        from app.utils.platform import file_manager_name, reveal

        ok, reason = reveal(path)
        if not ok:
            self._state.report(
                f"Could not open {file_manager_name()}: {reason}", "warning"
            )

    def export_srt(self) -> None:
        if not self._state.has_captions:
            self._state.report("There are no subtitles to export.", "warning")
            return
        target = self._target(".srt")
        try:
            from app.srt.writer import write_srt

            write_srt(target, self._state.segments)
        except PermissionError as exc:
            self._state.raise_error(
                OperationError(
                    ErrorCode.FILE_PERMISSION_DENIED,
                    "The subtitle file could not be saved to that folder.",
                    reason="macOS denied permission to write there.",
                    recommended_action="Choose a folder inside your home directory.",
                    details=str(exc),
                    operation="export_srt",
                )
            )
            return
        except OSError as exc:
            from app.core.status import capture

            self._state.raise_error(
                capture(
                    exc,
                    ErrorCode.AUDIO_EXPORT_FAILED,
                    user_message="The subtitle file could not be saved.",
                    recommended_action="Choose a different destination folder.",
                    operation="export_srt",
                )
            )
            return
        self._state.report(f"Exported {target.name}", "success")
        self._show_result(target)
