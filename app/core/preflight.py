"""Pre-flight checks (§10).

Everything that could stop a generation is verified *before* the first second of
audio is synthesised: the subtitles, the engine, the voice model, FFmpeg, the
output folder and the free disk space. A failure here costs the user a second,
not ten minutes.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from app.core.models import Segment
from app.core.status import ErrorCode, OperationError, Severity
from app.core.timecode import format_display
from app.core.validation import validate

#: Rough upper bound on bytes per second of 24 kHz 16-bit mono, with room for
#: the cache copies written alongside the export.
BYTES_PER_SECOND = 24_000 * 2
SAFETY_FACTOR = 4


@dataclass
class Check:
    """One line of the pre-flight list."""

    key: str
    label: str
    passed: bool
    detail: str = ""
    error: OperationError | None = None

    @property
    def mark(self) -> str:
        return "✓" if self.passed else "✗"


@dataclass
class PreflightReport:
    checks: list[Check] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    @property
    def failures(self) -> list[Check]:
        return [check for check in self.checks if not check.passed]

    @property
    def first_error(self) -> OperationError | None:
        for check in self.checks:
            if not check.passed and check.error:
                return check.error
        return None

    def add(self, check: Check) -> Check:
        self.checks.append(check)
        return check


def run_preflight(
    segments: Sequence[Segment],
    engine_id: str,
    voice_id: str,
    output_path: Path | None,
    timeline_ms: int | None = None,
) -> PreflightReport:
    """Verify everything generation depends on. Never raises."""
    report = PreflightReport()

    _check_subtitles(report, segments, timeline_ms)
    engine = _check_engine(report, engine_id)
    _check_voice(report, engine, voice_id)
    _check_ffmpeg(report)
    _check_output(report, output_path, timeline_ms or _timeline_of(segments))

    return report


def _timeline_of(segments: Sequence[Segment]) -> int:
    return max((s.end_ms for s in segments), default=0)


def _check_subtitles(
    report: PreflightReport, segments: Sequence[Segment], timeline_ms: int | None
) -> None:
    if not segments:
        report.add(
            Check(
                "srt", "Subtitles", False, "No subtitles loaded",
                OperationError(
                    ErrorCode.SRT_EMPTY,
                    "There are no subtitles to narrate.",
                    reason="The project has no subtitle segments.",
                    recommended_action="Import an SRT file to get started.",
                    operation="preflight",
                ),
            )
        )
        return

    report.add(Check("srt", "Subtitles valid", True, f"{len(segments)} subtitles"))

    result = validate(segments)
    if result.errors:
        first = result.errors[0]
        code = (
            ErrorCode.SRT_TIMESTAMP_OVERLAP
            if first.code == "overlap"
            else ErrorCode.SRT_TIMESTAMP_INVALID
        )
        report.add(
            Check(
                "timeline", "Timeline consistent", False,
                f"{len(result.errors)} problem(s)",
                OperationError(
                    code,
                    f"The subtitle timings have {len(result.errors)} problem(s) that "
                    "would produce misaligned audio.",
                    reason=first.message,
                    recommended_action=first.suggestion,
                    operation="preflight",
                ),
            )
        )
    else:
        total = timeline_ms or _timeline_of(segments)
        report.add(
            Check("timeline", "Timeline consistent", True, format_display(total))
        )


def _check_engine(report: PreflightReport, engine_id: str):
    from app.tts.registry import engine as get_engine

    try:
        backend = get_engine(engine_id)
    except KeyError as exc:
        report.add(
            Check(
                "engine", "Speech engine", False, engine_id,
                OperationError(
                    ErrorCode.ENGINE_UNAVAILABLE,
                    f"The speech engine “{engine_id}” is not available.",
                    reason=str(exc),
                    recommended_action="Choose a different engine in Settings.",
                    operation="preflight",
                ),
            )
        )
        return None

    available, why = backend.is_available()
    if not available:
        report.add(
            Check(
                "engine", "Speech engine", False, backend.display_name,
                OperationError(
                    ErrorCode.ENGINE_UNAVAILABLE,
                    f"{backend.display_name} is not ready to generate speech.",
                    reason=why,
                    recommended_action="Run setup.sh to install the local speech engine.",
                    recoverable=False,
                    operation="preflight",
                ),
            )
        )
        return None

    report.add(Check("engine", f"{backend.display_name} available", True, "local"))
    return backend


def _check_voice(report: PreflightReport, backend, voice_id: str) -> None:
    if backend is None:
        report.add(Check("voice", "Voice model", False, "engine unavailable"))
        return

    voice = next((v for v in backend.voices() if v.identifier == voice_id), None)
    if voice is None:
        report.add(
            Check(
                "voice", "Voice model", False, voice_id,
                OperationError(
                    ErrorCode.VOICE_NOT_FOUND,
                    f"The voice “{voice_id}” does not exist in this engine.",
                    reason="It may have been renamed or removed.",
                    recommended_action="Pick a voice from the Voice library.",
                    operation="preflight",
                ),
            )
        )
        return

    installed = getattr(backend, "installed_voice_files", lambda: set())()
    if installed and voice_id not in installed:
        # Not fatal: the model downloads on first use. Say so rather than
        # letting the user wonder why generation stalls on a download.
        report.add(
            Check(
                "voice", f"Voice “{voice.name}”", True,
                "will download on first use (~few MB)",
            )
        )
    else:
        report.add(Check("voice", f"Voice “{voice.name}” ready", True, voice.gender))


def _check_ffmpeg(report: PreflightReport) -> None:
    path = shutil.which("ffmpeg")
    if not path:
        report.add(
            Check(
                "ffmpeg", "FFmpeg", False, "not found",
                OperationError(
                    ErrorCode.FFMPEG_NOT_FOUND,
                    "FFmpeg is not installed, so speech cannot be fitted to your "
                    "subtitle timings.",
                    reason="No “ffmpeg” executable was found on this Mac.",
                    recommended_action="Install it in Terminal with: brew install ffmpeg",
                    recoverable=False,
                    operation="preflight",
                ),
            )
        )
        return

    try:
        result = subprocess.run(
            [path, "-version"], capture_output=True, timeout=10, check=True
        )
        version = result.stdout.decode("utf-8", "replace").split()[2]
    except Exception:
        version = "installed"
    report.add(Check("ffmpeg", "FFmpeg available", True, version))


def _check_output(
    report: PreflightReport, output_path: Path | None, timeline_ms: int
) -> None:
    if output_path is None:
        report.add(Check("output", "Output folder", True, "chosen at export"))
        return

    folder = output_path.parent if output_path.suffix else output_path
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        report.add(
            Check(
                "output", "Output folder", False, str(folder),
                OperationError(
                    ErrorCode.OUTPUT_NOT_WRITABLE,
                    "The folder you chose for the export cannot be created.",
                    reason=str(exc),
                    recommended_action="Choose a different destination folder.",
                    operation="preflight",
                ),
            )
        )
        return

    if not os.access(folder, os.W_OK):
        report.add(
            Check(
                "output", "Output folder writable", False, str(folder),
                OperationError(
                    ErrorCode.FILE_PERMISSION_DENIED,
                    "This app does not have permission to write to that folder.",
                    reason=f"macOS denied write access to {folder}.",
                    recommended_action=(
                        "Choose a folder inside your home directory, or grant access "
                        "in System Settings ▸ Privacy & Security ▸ Files and Folders."
                    ),
                    operation="preflight",
                ),
            )
        )
        return

    report.add(Check("output", "Output folder writable", True, folder.name))

    needed = int(timeline_ms / 1000 * BYTES_PER_SECOND * SAFETY_FACTOR)
    try:
        free = shutil.disk_usage(folder).free
    except OSError:
        report.add(Check("disk", "Disk space", True, "could not be measured"))
        return

    if free < needed:
        report.add(
            Check(
                "disk", "Disk space", False, f"{free / 1e9:.1f} GB free",
                OperationError(
                    ErrorCode.DISK_SPACE_LOW,
                    "Your Mac does not have enough free disk space to finish this "
                    "narration.",
                    reason=(
                        f"About {needed / 1e6:.0f} MB is needed but only "
                        f"{free / 1e6:.0f} MB is free on that volume."
                    ),
                    recommended_action="Free up space, or export to another drive.",
                    operation="preflight",
                ),
            )
        )
        return

    report.add(Check("disk", "Disk space sufficient", True, f"{free / 1e9:.0f} GB free"))
