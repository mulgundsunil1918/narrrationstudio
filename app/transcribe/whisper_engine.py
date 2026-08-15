"""Whisper transcription, supervised as a child process.

faster-whisper rather than openai-whisper because it runs on CTranslate2, needs
no PyTorch of its own, and is several times quicker on a CPU.

Nothing here imports faster_whisper. The model runs in
:mod:`app.transcribe.worker_main`, for the reason documented there: PyTorch (via
Kokoro) and CTranslate2 each bundle an Intel OpenMP runtime, and two of those in
one process abort it outright when a model loads. This module spawns that child,
reads its JSON stream, and turns anything that goes wrong into an error someone
can act on.

Segments come back already timed, which is the whole point — the result is a
caption list the rest of the app treats exactly like an imported SRT.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

from app.core.errors import StudioError
from app.transcribe.base import (
    ProgressCallback,
    TranscribeRequest,
    Transcriber,
    TranscriptionResult,
    Utterance,
    extract_audio,
    has_audio_track,
    media_duration_ms,
    register,
)

logger = logging.getLogger(__name__)

#: What each model costs and what it gives back. Sizes are the CTranslate2
#: downloads, which happen once and then work offline.
MODELS: tuple[dict, ...] = (
    {"size": "tiny",   "label": "Fastest",  "download": "~75 MB",
     "note": "Quick, and rough. Good for checking this works."},
    {"size": "base",   "label": "Fast",     "download": "~145 MB",
     "note": "Noticeably better than Fastest, and still quick."},
    {"size": "small",  "label": "Balanced", "download": "~490 MB",
     "note": "The best trade-off for most narration. Recommended."},
    {"size": "medium", "label": "Accurate", "download": "~1.5 GB",
     "note": "Better with accents and specialist words. Slower."},
)

DEFAULT_MODEL = "small"

#: How long to wait for a cancelled child to stop politely before killing it.
TERMINATE_GRACE_SECONDS = 3.0

#: How often to look for a cancellation while the child is producing nothing.
CANCEL_POLL_SECONDS = 0.2

#: The project root, so the child can import ``app`` whatever the cwd is.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _hf_cache_root() -> Path:
    """Where downloaded models live, without importing huggingface_hub."""
    if os.environ.get("HF_HUB_CACHE"):
        return Path(os.environ["HF_HUB_CACHE"])
    if os.environ.get("HF_HOME"):
        return Path(os.environ["HF_HOME"]) / "hub"
    return Path.home() / ".cache" / "huggingface" / "hub"


class WhisperTranscriber(Transcriber):
    identifier = "whisper"
    display_name = "Whisper"

    # -- availability ----------------------------------------------------

    def is_available(self) -> tuple[bool, str]:
        """Whether the child could run, checked without loading anything heavy."""
        from importlib.util import find_spec

        try:
            if find_spec("faster_whisper") is None:
                return False, "The transcription engine is not installed."
        except (ImportError, ValueError):
            return False, "The transcription engine is not installed."
        if not sys.executable:
            return False, "This build cannot start the transcription process."
        return True, ""

    def installed_models(self) -> set[str]:
        """Model sizes already downloaded. Reads the cache; never goes online."""
        root = _hf_cache_root()
        if not root.exists():
            return set()
        found: set[str] = set()
        for entry in root.glob("models--Systran--faster-whisper-*"):
            if any(entry.glob("snapshots/*/model.bin")):
                found.add(entry.name.rsplit("-", 1)[-1])
        return found

    # -- transcription ---------------------------------------------------

    def transcribe(
        self,
        request: TranscribeRequest,
        on_progress: ProgressCallback | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> TranscriptionResult:
        source = Path(request.media_path)
        if not source.exists():
            raise StudioError(
                f"“{source.name}” could not be found.",
                suggestion="Check the file is still where you dropped it from.",
            )

        available, why = self.is_available()
        if not available:
            raise StudioError(
                "This copy of the app cannot listen to videos.",
                reason=why,
                suggestion="Reinstall the app, or import a subtitle file instead.",
            )

        # Catch the silent screen recording before spending minutes on it.
        if not has_audio_track(source):
            raise StudioError(
                f"“{source.name}” has no sound in it.",
                reason="The file contains no audio track at all.",
                suggestion=(
                    "There is nothing to transcribe. For a silent recording, write "
                    "the script instead and bring it here as text."
                ),
            )

        started = time.monotonic()
        model_size = request.model_size or DEFAULT_MODEL

        if on_progress:
            on_progress(None, "Reading the audio out of your video…")
        audio_path = extract_audio(source)
        total_ms = media_duration_ms(audio_path) or media_duration_ms(source)

        if on_progress:
            if model_size in self.installed_models():
                on_progress(None, "Starting up…")
            else:
                on_progress(
                    None,
                    "Downloading what it needs to listen. This happens once, "
                    "then it works offline.",
                )

        try:
            result = self._run_child(
                audio_path, model_size, request.language,
                total_ms, on_progress, should_cancel,
            )
        finally:
            self._discard(audio_path)

        result.duration_ms = total_ms
        result.model = model_size
        result.seconds_taken = time.monotonic() - started
        logger.info(
            "Transcribed %s: %d utterances, language=%s, %.1fs",
            source.name, len(result.utterances), result.language, result.seconds_taken,
        )
        return result

    # -- the child -------------------------------------------------------

    def _run_child(
        self,
        audio_path: Path,
        model_size: str,
        language: str,
        total_ms: int,
        on_progress: ProgressCallback | None,
        should_cancel: Callable[[], bool] | None,
    ) -> TranscriptionResult:
        # An argument array, never a shell string: nothing here is interpreted,
        # so a file name cannot become a command.
        command = [
            sys.executable, "-u", "-m", "app.transcribe.worker_main",
            str(audio_path), "--model", model_size,
        ]
        if language:
            command += ["--language", language]

        environment = dict(os.environ)
        existing = environment.get("PYTHONPATH", "")
        environment["PYTHONPATH"] = (
            f"{_PROJECT_ROOT}{os.pathsep}{existing}" if existing else str(_PROJECT_ROOT)
        )

        utterances: list[Utterance] = []
        warnings: list[str] = []
        language_detected = ""
        language_probability = 0.0
        cancelled = False
        ended_cleanly = False
        failure: dict | None = None

        # stderr goes to a file rather than a second pipe: reading two pipes from
        # one thread deadlocks as soon as either fills up, and if the child dies
        # natively its last words are the only clue to why.
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as errors:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=errors,
                cwd=str(_PROJECT_ROOT),
                env=environment,
                text=True,
                bufsize=1,
            )
            try:
                assert process.stdout is not None
                # A reader thread rather than iterating the pipe directly.
                # Reading inline only notices a cancellation between lines, and
                # the longest silence is the first-time model download — exactly
                # when someone is most likely to give up and press Stop. This
                # way the wait is bounded no matter what the child is doing.
                for line in self._lines(process, should_cancel):
                    if line is None:
                        cancelled = True
                        break
                    try:
                        message = json.loads(line)
                    except json.JSONDecodeError:
                        # A stray print is not a reason to fail the run.
                        logger.debug("transcriber said: %s", line.rstrip())
                        continue

                    event = message.get("event")
                    if event == "ready":
                        language_detected = message.get("language", "")
                        language_probability = message.get("language_probability", 0.0)
                        if on_progress:
                            # Still a status line: nothing has been heard yet.
                            on_progress(None, "Listening…")
                    elif event == "utterance":
                        utterances.append(
                            Utterance(
                                start_ms=int(round(message["start"] * 1000)),
                                end_ms=int(round(message["end"] * 1000)),
                                text=message["text"],
                            )
                        )
                        if on_progress:
                            fraction = (
                                min(1.0, (message["end"] * 1000) / total_ms)
                                if total_ms else None
                            )
                            on_progress(fraction, message["text"])
                    elif event == "error":
                        failure = message
                    elif event == "done":
                        pass
                # Only reached by running out of output, not by the break above
                # and not by an exception.
                ended_cleanly = not cancelled
            finally:
                # Anything else — a cancellation, or a bug in the loop above —
                # leaves the child running, and an unconditional wait() on a
                # live child hangs the app for the length of the video.
                if not ended_cleanly:
                    self._stop(process)
                returncode = process.wait()
                if process.stdout is not None:
                    process.stdout.close()

            errors.seek(0)
            stderr_text = errors.read()[-4000:]

        if cancelled:
            warnings.append(
                "Transcription was stopped early; only part of the video was read."
            )
        elif failure is not None:
            raise self._describe(failure, stderr_text)
        elif returncode != 0:
            raise self._crashed(returncode, stderr_text)

        return TranscriptionResult(
            utterances=utterances,
            language=language_detected,
            language_probability=language_probability,
            warnings=warnings,
        )

    def _lines(
        self,
        process: subprocess.Popen,
        should_cancel: Callable[[], bool] | None,
    ):
        """Yield the child's output lines, or a single ``None`` if cancelled.

        The blocking read happens on its own thread so this loop stays free to
        notice a cancellation while the child is silent.
        """
        lines: queue.Queue[str | None] = queue.Queue()

        def pump() -> None:
            try:
                assert process.stdout is not None
                for line in process.stdout:
                    lines.put(line)
            except (ValueError, OSError):
                pass    # the pipe was closed under us; the exit code tells the story
            finally:
                lines.put(None)   # sentinel: the child has stopped talking

        reader = threading.Thread(target=pump, name="transcriber-reader", daemon=True)
        reader.start()

        while True:
            if should_cancel and should_cancel():
                yield None
                return
            try:
                line = lines.get(timeout=CANCEL_POLL_SECONDS)
            except queue.Empty:
                continue
            if line is None:
                return
            yield line

    def _stop(self, process: subprocess.Popen) -> None:
        """Cancel for real: ask, then insist."""
        process.terminate()
        try:
            process.wait(timeout=TERMINATE_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            logger.warning("Transcriber ignored terminate; killing it")
            process.kill()

    def _describe(self, failure: dict, stderr_text: str) -> StudioError:
        kind = failure.get("kind", "")
        detail = failure.get("detail", "") or stderr_text
        if kind == "unavailable":
            return StudioError(
                "This copy of the app cannot listen to videos.",
                reason=failure.get("message", ""),
                suggestion="Reinstall the app, or import a subtitle file instead.",
                detail=detail,
            )
        if kind == "model":
            return StudioError(
                "The transcription model could not be loaded.",
                reason=failure.get("message", ""),
                suggestion=(
                    "Check your internet connection for the first-time download, "
                    "then try again. Later runs work offline."
                ),
                detail=detail,
            )
        return StudioError(
            "The audio could not be transcribed.",
            reason=failure.get("message", ""),
            suggestion="Try again, or choose a quicker transcription setting.",
            detail=detail,
        )

    def _crashed(self, returncode: int, stderr_text: str) -> StudioError:
        reason = "The transcription process stopped unexpectedly."
        if "OMP" in stderr_text or "libiomp" in stderr_text:
            # Should be impossible now the child keeps PyTorch out, but if it
            # ever recurs, say so plainly instead of leaving a blank failure.
            reason = (
                "Two conflicting maths libraries were loaded in the same process."
            )
        return StudioError(
            "Transcription stopped before it finished.",
            reason=reason,
            suggestion=(
                "Try again with a quicker setting. If it keeps happening, import "
                "a subtitle file for this video instead."
            ),
            detail=f"exit code {returncode}\n\n{stderr_text}",
        )

    def _discard(self, audio_path: Path) -> None:
        try:
            audio_path.unlink(missing_ok=True)
            audio_path.parent.rmdir()
        except OSError:
            pass  # a leftover temp file is not worth troubling anyone with


register("whisper", WhisperTranscriber)
