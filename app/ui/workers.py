"""Background workers.

All synthesis runs off the UI thread so the window never freezes. Every worker
finishes in exactly one of three ways — ``finished``, ``failed`` or
``cancelled`` — and each is wired to a visible state, so a worker cannot end
without the user being told.

A stall watchdog fires if no progress arrives for a while, because a hung model
must not look like an idle one.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import numpy as np
from PySide6.QtCore import QObject, Qt, QThread, Signal

from app.core.status import ErrorCode, OperationError, capture
from app.pipeline import (
    CancellationToken,
    GenerationOutcome,
    GenerationSettings,
    GroupProgress,
    generate,
)

logger = logging.getLogger(__name__)

#: No progress for this long means something is wrong or unusually slow.
STALL_SECONDS = 90.0


class GenerationWorker(QObject):
    """Runs the narration pipeline and reports progress, faults and completion."""

    progress = Signal(object)        # GroupProgress
    finished = Signal(object)        # GenerationOutcome
    failed = Signal(object)          # OperationError
    cancelled = Signal(object)       # GenerationOutcome | None
    stalled = Signal(float)          # seconds since the last progress event

    def __init__(
        self,
        segments,
        settings: GenerationSettings,
        only_groups: list[int] | None = None,
    ) -> None:
        super().__init__()
        self._segments = list(segments)
        self._settings = settings
        self._only_groups = only_groups
        self.token = CancellationToken()
        self._last_progress = time.monotonic()
        self._stall_reported = False

    def cancel(self) -> None:
        self.token.cancel()

    @property
    def seconds_since_progress(self) -> float:
        return time.monotonic() - self._last_progress

    def check_stall(self) -> None:
        """Called by a timer on the UI thread; emits once per stall episode."""
        if self._stall_reported:
            return
        if self.seconds_since_progress > STALL_SECONDS:
            self._stall_reported = True
            self.stalled.emit(self.seconds_since_progress)

    def _on_progress(self, item: GroupProgress) -> None:
        self._last_progress = time.monotonic()
        self._stall_reported = False
        self.progress.emit(item)

    def run(self) -> None:
        """Entry point on the worker thread. Never raises out of here."""
        try:
            outcome = generate(
                self._segments,
                self._settings,
                on_progress=self._on_progress,
                token=self.token,
                only_groups=self._only_groups,
            )
        except Exception as exc:
            logger.exception("Generation failed")
            self.failed.emit(
                capture(
                    exc,
                    ErrorCode.TTS_GENERATION_FAILED,
                    user_message="The narration could not be generated.",
                    recommended_action=(
                        "Try again, or choose a different voice. If it keeps "
                        "failing, open Settings ▸ Advanced to check the engine."
                    ),
                    operation="generate",
                )
            )
            return

        if outcome.cancelled:
            self.cancelled.emit(outcome)
            return
        self.finished.emit(outcome)


class PreviewWorker(QObject):
    """Renders a short voice sample for the voice library."""

    finished = Signal(object, int)   # (audio, sample_rate)
    failed = Signal(object)          # OperationError

    def __init__(self, engine_id: str, voice_id: str, text: str, speed: float = 1.0) -> None:
        super().__init__()
        self._engine_id = engine_id
        self._voice_id = voice_id
        self._text = text
        self._speed = speed

    def run(self) -> None:
        try:
            from app.tts.base import GenerationRequest
            from app.tts.registry import engine as get_engine

            backend = get_engine(self._engine_id)
            available, why = backend.is_available()
            if not available:
                self.failed.emit(
                    OperationError(
                        ErrorCode.ENGINE_UNAVAILABLE,
                        "The speech engine is not ready, so this voice cannot be previewed.",
                        reason=why,
                        recommended_action="Run setup.sh to install the local speech engine.",
                        operation="voice_preview",
                    )
                )
                return

            voice = next(
                (v for v in backend.voices() if v.identifier == self._voice_id), None
            )
            lang_code = voice.lang_code if voice else "a"

            result = backend.generate(
                GenerationRequest(
                    text=self._text,
                    voice=self._voice_id,
                    lang_code=lang_code,
                    speed=self._speed,
                )
            )
            if result.is_empty:
                self.failed.emit(
                    OperationError(
                        ErrorCode.TTS_EMPTY_AUDIO,
                        f"“{self._voice_id}” produced no audio for the sample text.",
                        reason="The voice model returned an empty result.",
                        recommended_action="Try a different voice.",
                        operation="voice_preview",
                    )
                )
                return
            self.finished.emit(result.audio, result.sample_rate)
        except Exception as exc:
            self.failed.emit(
                capture(
                    exc,
                    ErrorCode.VOICE_MODEL_LOAD_FAILED,
                    user_message=f"“{self._voice_id}” could not be previewed.",
                    recommended_action="Try a different voice, or check Settings ▸ Models.",
                    operation="voice_preview",
                )
            )


class ExportWorker(QObject):
    """Writes the finished timeline to disk in the requested formats."""

    finished = Signal(object)   # list[Path]
    failed = Signal(object)     # OperationError

    def __init__(
        self,
        audio: np.ndarray,
        sample_rate: int,
        wav_path: Path,
        also_mp3: bool = False,
    ) -> None:
        super().__init__()
        self._audio = audio
        self._sample_rate = sample_rate
        self._wav_path = wav_path
        self._also_mp3 = also_mp3

    def run(self) -> None:
        from app.audio.assemble import write_mp3, write_wav
        from app.core.errors import AudioError

        written: list[Path] = []

        # Check writability up front. Sound libraries report a permission
        # problem as a generic encoding failure, which would tell the user the
        # wrong thing about a very fixable situation.
        folder = self._wav_path.parent
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.failed.emit(
                OperationError(
                    ErrorCode.OUTPUT_NOT_WRITABLE,
                    "That destination folder could not be created.",
                    reason=str(exc),
                    recommended_action="Choose a different destination folder.",
                    details=str(exc),
                    operation="export",
                )
            )
            return
        if not os.access(folder, os.W_OK):
            self.failed.emit(
                OperationError(
                    ErrorCode.FILE_PERMISSION_DENIED,
                    "The narration could not be saved to that folder.",
                    reason=f"macOS does not allow this app to write to {folder}.",
                    recommended_action=(
                        "Choose a folder inside your home directory, such as "
                        "Desktop or Documents."
                    ),
                    operation="export",
                )
            )
            return

        try:
            written.append(write_wav(self._wav_path, self._audio, self._sample_rate))
        except PermissionError as exc:
            self.failed.emit(
                OperationError(
                    ErrorCode.FILE_PERMISSION_DENIED,
                    "The narration could not be saved to that folder.",
                    reason="macOS denied permission to write there.",
                    recommended_action="Choose a folder inside your home directory.",
                    details=str(exc),
                    operation="export",
                )
            )
            return
        except OSError as exc:
            low_space = getattr(exc, "errno", None) == 28
            self.failed.emit(
                OperationError(
                    ErrorCode.DISK_SPACE_LOW if low_space else ErrorCode.AUDIO_EXPORT_FAILED,
                    (
                        "Your Mac ran out of disk space while saving the narration."
                        if low_space
                        else "The narration could not be saved."
                    ),
                    reason=str(exc),
                    recommended_action=(
                        "Free up space, or export to another drive."
                        if low_space
                        else "Try a different destination folder."
                    ),
                    details=str(exc),
                    operation="export",
                )
            )
            return
        except Exception as exc:
            self.failed.emit(
                capture(
                    exc,
                    ErrorCode.AUDIO_EXPORT_FAILED,
                    user_message="The narration could not be saved.",
                    recommended_action="Try a different destination folder.",
                    operation="export",
                )
            )
            return

        if self._also_mp3:
            try:
                written.append(
                    write_mp3(self._wav_path.with_suffix(".mp3"), self._wav_path)
                )
            except AudioError as exc:
                # The WAV succeeded; report the MP3 as a warning, not a failure.
                self.failed.emit(
                    OperationError(
                        ErrorCode.AUDIO_ENCODING_FAILED,
                        "The WAV was saved, but the MP3 could not be created.",
                        reason=getattr(exc, "message", str(exc)),
                        recommended_action=getattr(exc, "suggestion", "Use the WAV instead."),
                        details=getattr(exc, "detail", ""),
                        operation="export",
                    )
                )
                return

        self.finished.emit(written)


class _ThreadRegistry(QObject):
    """Owns every running worker thread and disposes of them on the UI thread.

    Two rules earned the hard way, both from real crashes:

    1. ``moveToThread`` creates no Python reference. A worker held only by a
       local variable is collected the moment the calling method returns, and
       the operation stops with no result and no error.
    2. Cleanup must not run on the thread that is dying. Connecting
       ``thread.finished`` to ``worker.deleteLater`` — the pattern the Qt docs
       show for C++ — makes Shiboken tear down the Python wrapper *from the
       worker thread* while the main thread is destroying the QThread wrapper.
       Both then contend for the GIL over freed memory and the process
       segfaults. So nothing is ``deleteLater``-ed here: the registry drops its
       reference on the UI thread once the thread has genuinely stopped, and
       ordinary refcounting does the rest.
    """

    def __init__(self) -> None:
        super().__init__()
        self._entries: dict[QThread, QObject] = {}

    def track(self, thread: QThread, worker: QObject) -> None:
        self._entries[thread] = worker
        # Queued, and bound to this object which lives on the UI thread, so the
        # slot cannot run on the worker thread.
        thread.finished.connect(self._on_finished, Qt.ConnectionType.QueuedConnection)

    def _on_finished(self) -> None:
        thread = self.sender()
        if not isinstance(thread, QThread):
            return
        # The thread has signalled it is done; wait for the OS thread to exit
        # before releasing the objects it was running.
        thread.wait(5000)
        self._entries.pop(thread, None)

    def shutdown(self, timeout_ms: int = 5000) -> None:
        for thread, worker in list(self._entries.items()):
            cancel = getattr(worker, "cancel", None)
            if callable(cancel):
                cancel()
            thread.quit()
            thread.wait(timeout_ms)
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)


_registry: _ThreadRegistry | None = None


def registry() -> _ThreadRegistry:
    """The registry, created lazily on the UI thread that first uses it."""
    global _registry
    if _registry is None:
        _registry = _ThreadRegistry()
    return _registry


def run_in_thread(worker: QObject, owner: QObject) -> QThread:
    """Move ``worker`` onto a new thread and start it.

    The thread is deliberately parentless: parenting it to a widget lets Qt
    destroy it while it is still running when that widget goes away. The
    registry owns it instead.
    """
    thread = QThread()
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    registry().track(thread, worker)

    for signal_name in ("finished", "failed", "cancelled"):
        signal = getattr(worker, signal_name, None)
        if signal is not None:
            signal.connect(thread.quit, Qt.ConnectionType.QueuedConnection)

    thread.start()
    return thread


def wait_for_threads(timeout_ms: int = 5000) -> None:
    """Stop every running worker thread cleanly, used when the window closes."""
    registry().shutdown(timeout_ms)


def active_thread_count() -> int:
    return len(registry())
