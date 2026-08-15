"""Listen to a video and write its script.

Two phases in one window. First it asks — because the first run downloads a
model and because a long video takes real minutes, and springing either on
someone is how an app earns a reputation for hanging. Then it works, showing the
words as they are recognised: a transcript filling in front of you is far better
evidence that something is happening than any spinner.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QPlainTextEdit,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)

from app.core.status import OperationError
from app.ui.widgets.common import (
    Card,
    PrimaryButton,
    SecondaryButton,
    Segmented,
    caption,
    label,
    muted,
    section_label,
    title,
)
from app.ui.workers import TranscribeWorker, run_in_thread

logger = logging.getLogger(__name__)


class TranscribeDialog(QDialog):
    """Runs transcription for one file and returns the recognised utterances."""

    #: Emitted instead of raising, so the caller's single error handler shows it.
    failed = Signal(object)   # OperationError

    def __init__(self, media_path: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._media_path = Path(media_path)
        self._worker: TranscribeWorker | None = None
        self._thread = None
        self._done = False
        self.result_data = None      # TranscriptionResult, once it succeeds
        self.error: OperationError | None = None

        self.setWindowTitle("Write the script from this video")
        self.setModal(True)
        self.setMinimumSize(640, 560)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 22, 24, 20)
        outer.setSpacing(14)

        outer.addWidget(title("Let the app write the script"))
        outer.addWidget(
            muted(
                f"“{self._media_path.name}” has sound on it, so the words and their "
                "timings can be taken straight from the video. Everything happens on "
                "this Mac — the video is never uploaded anywhere.",
                wrap=True,
            )
        )

        self._setup = self._build_setup()
        outer.addWidget(self._setup)

        self._progress = QProgressBar()
        self._progress.setRange(0, 0)          # indeterminate until a length is known
        self._progress.setTextVisible(False)
        self._progress.setFixedHeight(6)
        self._progress.setVisible(False)
        outer.addWidget(self._progress)

        self._status = label("", "Body")
        self._status.setVisible(False)
        outer.addWidget(self._status)

        outer.addWidget(section_label("What it is hearing"))
        self._transcript = QPlainTextEdit()
        self._transcript.setReadOnly(True)
        self._transcript.setPlaceholderText(
            "The words will appear here as they are recognised."
        )
        outer.addWidget(self._transcript, 1)

        outer.addLayout(self._build_actions())

    # -- phase one: the ask ----------------------------------------------

    def _build_setup(self) -> QWidget:
        from app.transcribe.whisper_engine import DEFAULT_MODEL, MODELS

        card = Card(quiet=True)
        card.body.setSpacing(9)
        card.add(section_label("How careful should it be?"))

        self._models = {entry["size"]: entry for entry in MODELS}
        self._choice = Segmented(
            [(entry["size"], entry["label"]) for entry in MODELS], DEFAULT_MODEL
        )
        self._choice.changed.connect(self._describe_choice)
        card.add(self._choice)

        self._choice_note = caption("", wrap=True)
        card.add(self._choice_note)
        self._describe_choice(DEFAULT_MODEL)
        return card

    def _installed_models(self) -> set[str]:
        try:
            from app.transcribe import transcriber

            return transcriber("whisper").installed_models()
        except Exception:
            # Only used to word a note; never worth failing the dialog over.
            return set()

    def _describe_choice(self, size: str) -> None:
        entry = self._models.get(size)
        if entry is None:
            return
        note = entry["note"]
        if size not in self._installed_models():
            note += (
                f"  A one-off download of about {entry['download'].lstrip('~')} "
                "happens the first time you use it; after that it works offline."
            )
        self._choice_note.setText(note)

    def _build_actions(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(9)
        row.addStretch(1)

        self._cancel = SecondaryButton("Cancel")
        self._cancel.clicked.connect(self.reject)
        row.addWidget(self._cancel)

        self._start = PrimaryButton("Start Listening")
        self._start.clicked.connect(self.start)
        row.addWidget(self._start)
        return row

    # -- phase two: the work ---------------------------------------------

    def start(self) -> None:
        if self._worker is not None:
            return

        self._setup.setEnabled(False)
        self._start.setVisible(False)
        self._cancel.setText("Stop")
        self._progress.setVisible(True)
        self._status.setVisible(True)
        self._set_status("Getting ready…")

        worker = TranscribeWorker(self._media_path, model_size=self._choice.current())
        # Bound methods, queued: a lambda here would carry no thread affinity and
        # the handler would run on the worker thread, where touching widgets
        # fails without saying so.
        worker.progress.connect(self._on_progress, Qt.ConnectionType.QueuedConnection)
        worker.finished.connect(self._on_finished, Qt.ConnectionType.QueuedConnection)
        worker.failed.connect(self._on_failed, Qt.ConnectionType.QueuedConnection)
        worker.cancelled.connect(self._on_cancelled, Qt.ConnectionType.QueuedConnection)
        self._worker = worker
        self._thread = run_in_thread(worker, self)

    def _set_status(self, text: str) -> None:
        self._status.setText(text)

    def _on_progress(self, fraction, message: str) -> None:
        # No fraction means this is a status line, not something that was said.
        if fraction is None:
            self._progress.setRange(0, 0)
            self._set_status(message)
            return
        if self._done:
            return   # a straggler must not undo the finished state

        self._progress.setRange(0, 1000)
        self._progress.setValue(int(min(1.0, max(0.0, fraction)) * 1000))
        self._set_status(f"Listening… {int(fraction * 100)}% of the way through")
        self._transcript.appendPlainText(message)

    def _on_finished(self, result) -> None:
        self._worker = None
        self._done = True
        self.result_data = result
        if result.is_empty:
            from app.core.status import ErrorCode

            self._fail(
                OperationError(
                    ErrorCode.TRANSCRIBE_NO_SPEECH,
                    f"No speech was found in “{self._media_path.name}”.",
                    reason=(
                        "The file has an audio track, but nothing in it was "
                        "recognised as speech."
                    ),
                    recommended_action=(
                        "If this is a silent screen recording, write the script "
                        "instead and import it as text."
                    ),
                    operation="transcribe",
                )
            )
            return
        self._progress.setRange(0, 1000)
        self._progress.setValue(1000)
        self._set_status(
            f"Heard {len(result.utterances)} lines in "
            f"{result.seconds_taken:.0f} seconds."
        )
        # Let the completed state be visible for a moment rather than vanishing.
        QTimer.singleShot(450, self.accept)

    def _on_failed(self, error: OperationError) -> None:
        self._worker = None
        self._fail(error)

    def _fail(self, error: OperationError) -> None:
        self.error = error
        self.failed.emit(error)
        self.reject()

    def _on_cancelled(self) -> None:
        self._worker = None
        self.reject()

    # -- stopping ---------------------------------------------------------

    def reject(self) -> None:
        """Cancel means cancel: stop the worker before the window closes."""
        worker = self._worker
        if worker is not None:
            self._worker = None
            worker.cancel()
            self._set_status("Stopping…")
        super().reject()

    def closeEvent(self, event) -> None:
        worker = self._worker
        if worker is not None:
            worker.cancel()
            self._worker = None
        super().closeEvent(event)
