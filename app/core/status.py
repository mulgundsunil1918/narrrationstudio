"""Structured status, error codes, and the operation state machine.

Every failure anywhere in the app becomes an :class:`OperationError` carrying a
code, a sentence for the user, a reason, a recommended action, and the raw
technical detail. The UI renders the first four and hides the last behind
"View Technical Details". Nothing is ever swallowed: a bare ``except: pass`` is
a bug, and :func:`capture` exists so there is never a reason to write one.
"""

from __future__ import annotations

import traceback
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Generic, TypeVar


class ErrorCode(str, Enum):
    """Stable identifiers for every way an operation can fail."""

    # -- subtitles ---------------------------------------------------
    SRT_INVALID = "SRT_INVALID"
    SRT_EMPTY = "SRT_EMPTY"
    SRT_TIMESTAMP_INVALID = "SRT_TIMESTAMP_INVALID"
    SRT_TIMESTAMP_OVERLAP = "SRT_TIMESTAMP_OVERLAP"
    SRT_UNSUPPORTED = "SRT_UNSUPPORTED"

    # -- voices and models -------------------------------------------
    VOICE_NOT_FOUND = "VOICE_NOT_FOUND"
    VOICE_MODEL_MISSING = "VOICE_MODEL_MISSING"
    VOICE_MODEL_LOAD_FAILED = "VOICE_MODEL_LOAD_FAILED"
    ENGINE_UNAVAILABLE = "ENGINE_UNAVAILABLE"

    # -- synthesis ---------------------------------------------------
    TTS_GENERATION_FAILED = "TTS_GENERATION_FAILED"
    TTS_EMPTY_AUDIO = "TTS_EMPTY_AUDIO"
    TTS_TIMEOUT = "TTS_TIMEOUT"

    # -- audio -------------------------------------------------------
    AUDIO_PROCESSING_FAILED = "AUDIO_PROCESSING_FAILED"
    AUDIO_ENCODING_FAILED = "AUDIO_ENCODING_FAILED"
    AUDIO_EXPORT_FAILED = "AUDIO_EXPORT_FAILED"

    FFMPEG_NOT_FOUND = "FFMPEG_NOT_FOUND"
    FFMPEG_FAILED = "FFMPEG_FAILED"

    # -- video -------------------------------------------------------
    VIDEO_IMPORT_FAILED = "VIDEO_IMPORT_FAILED"
    VIDEO_AUDIO_EXTRACTION_FAILED = "VIDEO_AUDIO_EXTRACTION_FAILED"
    VIDEO_UNSUPPORTED = "VIDEO_UNSUPPORTED"

    # -- transcription -----------------------------------------------
    TRANSCRIBE_UNAVAILABLE = "TRANSCRIBE_UNAVAILABLE"
    TRANSCRIBE_MODEL_FAILED = "TRANSCRIBE_MODEL_FAILED"
    TRANSCRIBE_NO_AUDIO = "TRANSCRIBE_NO_AUDIO"
    TRANSCRIBE_NO_SPEECH = "TRANSCRIBE_NO_SPEECH"
    TRANSCRIBE_FAILED = "TRANSCRIBE_FAILED"

    # -- projects ----------------------------------------------------
    PROJECT_LOAD_FAILED = "PROJECT_LOAD_FAILED"
    PROJECT_SAVE_FAILED = "PROJECT_SAVE_FAILED"

    # -- filesystem --------------------------------------------------
    FILE_NOT_FOUND = "FILE_NOT_FOUND"
    FILE_PERMISSION_DENIED = "FILE_PERMISSION_DENIED"
    DISK_SPACE_LOW = "DISK_SPACE_LOW"
    OUTPUT_NOT_WRITABLE = "OUTPUT_NOT_WRITABLE"

    # -- control flow ------------------------------------------------
    CANCELLED = "CANCELLED"
    UNKNOWN_ERROR = "UNKNOWN_ERROR"


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class OperationState(str, Enum):
    """The state machine every long-running operation moves through.

    Scattered boolean flags are what produce screens that appear stuck, so
    progress, generation and export all report exactly one of these.
    """

    IDLE = "idle"
    VALIDATING = "validating"
    READY = "ready"
    PROCESSING = "processing"
    GENERATING = "generating"
    COMPLETED = "completed"
    WARNING = "warning"
    ERROR = "error"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in (
            OperationState.COMPLETED,
            OperationState.WARNING,
            OperationState.ERROR,
            OperationState.CANCELLED,
        )

    @property
    def is_busy(self) -> bool:
        return self in (
            OperationState.VALIDATING,
            OperationState.PROCESSING,
            OperationState.GENERATING,
        )

    @property
    def label(self) -> str:
        return {
            OperationState.IDLE: "Ready",
            OperationState.VALIDATING: "Checking…",
            OperationState.READY: "Ready to generate",
            OperationState.PROCESSING: "Working…",
            OperationState.GENERATING: "Generating narration…",
            OperationState.COMPLETED: "Completed",
            OperationState.WARNING: "Completed with warnings",
            OperationState.ERROR: "Failed",
            OperationState.CANCELLED: "Cancelled",
        }[self]


#: What the user should be offered when a given failure happens.
ACTIONS: dict[ErrorCode, tuple[str, ...]] = {
    ErrorCode.VOICE_MODEL_MISSING: ("retry", "change_voice", "details"),
    ErrorCode.VOICE_MODEL_LOAD_FAILED: ("retry", "change_voice", "details"),
    ErrorCode.VOICE_NOT_FOUND: ("change_voice", "details"),
    ErrorCode.ENGINE_UNAVAILABLE: ("open_settings", "details"),
    ErrorCode.TTS_GENERATION_FAILED: ("retry", "change_voice", "details"),
    ErrorCode.TTS_EMPTY_AUDIO: ("retry", "change_voice", "details"),
    ErrorCode.TTS_TIMEOUT: ("retry", "cancel", "details"),
    ErrorCode.FFMPEG_NOT_FOUND: ("open_settings", "details"),
    ErrorCode.FFMPEG_FAILED: ("retry", "details"),
    ErrorCode.OUTPUT_NOT_WRITABLE: ("choose_folder", "details"),
    ErrorCode.DISK_SPACE_LOW: ("choose_folder", "details"),
    ErrorCode.FILE_PERMISSION_DENIED: ("choose_folder", "details"),
    ErrorCode.SRT_INVALID: ("choose_file", "details"),
    ErrorCode.SRT_EMPTY: ("choose_file", "details"),
    ErrorCode.SRT_UNSUPPORTED: ("choose_file", "details"),
    ErrorCode.VIDEO_UNSUPPORTED: ("choose_file", "details"),
    ErrorCode.TRANSCRIBE_UNAVAILABLE: ("open_settings", "details"),
    ErrorCode.TRANSCRIBE_MODEL_FAILED: ("retry", "details"),
    # No sound and no speech are both "wrong file" situations, so the useful
    # offer is another file rather than a pointless retry.
    ErrorCode.TRANSCRIBE_NO_AUDIO: ("choose_file", "details"),
    ErrorCode.TRANSCRIBE_NO_SPEECH: ("choose_file", "details"),
    ErrorCode.TRANSCRIBE_FAILED: ("retry", "choose_file", "details"),
}

ACTION_LABELS = {
    "retry": "Retry",
    "change_voice": "Change Voice",
    "details": "View Technical Details",
    "open_settings": "Open Settings",
    "choose_folder": "Choose Another Folder",
    "choose_file": "Choose Another File",
    "cancel": "Cancel",
}


@dataclass
class OperationError:
    """A failure the user can understand and act on."""

    code: ErrorCode
    #: One sentence, no jargon. "Unable to generate narration for one section."
    user_message: str
    #: Why it happened, in plain language.
    reason: str = ""
    #: What the user can do about it.
    recommended_action: str = ""
    #: The raw exception, command, stdout/stderr -- shown only on request.
    details: str = ""
    recoverable: bool = True
    severity: Severity = Severity.ERROR
    operation: str = ""
    #: Which narration segment this concerns, when applicable (1-based).
    segment: int | None = None
    at: datetime = field(default_factory=datetime.now)
    context: dict[str, str] = field(default_factory=dict)

    @property
    def headline(self) -> str:
        return self.user_message

    @property
    def actions(self) -> tuple[str, ...]:
        return ACTIONS.get(self.code, ("retry", "details"))

    def technical_report(self) -> str:
        """The full text behind "View Technical Details"."""
        lines = [
            f"Timestamp : {self.at.isoformat(timespec='seconds')}",
            f"Operation : {self.operation or '—'}",
            f"Error code: {self.code.value}",
            f"Severity  : {self.severity.value}",
            f"Recoverable: {'yes' if self.recoverable else 'no'}",
        ]
        if self.segment is not None:
            lines.append(f"Segment   : {self.segment}")
        for key, value in self.context.items():
            lines.append(f"{key:<10}: {value}")
        lines.append("")
        lines.append(f"Message   : {self.user_message}")
        if self.reason:
            lines.append(f"Reason    : {self.reason}")
        if self.recommended_action:
            lines.append(f"Action    : {self.recommended_action}")
        if self.details:
            lines.append("")
            lines.append("--- technical detail ---")
            lines.append(self.details)
        return "\n".join(lines)

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "message": self.reason or self.user_message,
            "userMessage": self.user_message,
            "details": self.details,
            "recoverable": self.recoverable,
            "severity": self.severity.value,
            "segment": self.segment,
        }


T = TypeVar("T")


@dataclass
class Result(Generic[T]):
    """Success or failure, never an unexplained None."""

    success: bool
    value: T | None = None
    error: OperationError | None = None
    warnings: list[OperationError] = field(default_factory=list)

    @classmethod
    def ok(cls, value: T, warnings: list[OperationError] | None = None) -> "Result[T]":
        return cls(success=True, value=value, warnings=warnings or [])

    @classmethod
    def fail(cls, error: OperationError) -> "Result[T]":
        return cls(success=False, error=error)

    def unwrap(self) -> T:
        if not self.success or self.value is None:
            raise RuntimeError(self.error.user_message if self.error else "Operation failed")
        return self.value


def capture(
    exc: BaseException,
    code: ErrorCode = ErrorCode.UNKNOWN_ERROR,
    *,
    user_message: str = "",
    reason: str = "",
    recommended_action: str = "",
    operation: str = "",
    segment: int | None = None,
    recoverable: bool = True,
    context: dict[str, str] | None = None,
) -> OperationError:
    """Turn any exception into a reportable error, keeping the traceback.

    Use this instead of swallowing an exception. The traceback survives in
    ``details`` where a developer can reach it, and the user sees a sentence.
    """
    detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return OperationError(
        code=code,
        user_message=user_message or "Something went wrong.",
        reason=reason or f"{type(exc).__name__}: {exc}",
        recommended_action=recommended_action or "Try the operation again.",
        details=detail,
        recoverable=recoverable,
        operation=operation,
        segment=segment,
        context=context or {},
    )


def from_pediaid_error(
    exc: Exception, code: ErrorCode, operation: str = "", segment: int | None = None
) -> OperationError:
    """Adapt the app's own friendly exceptions into the structured form."""
    return OperationError(
        code=code,
        user_message=getattr(exc, "message", str(exc)) or str(exc),
        reason=getattr(exc, "reason", "") or "",
        recommended_action=getattr(exc, "suggestion", "") or "",
        details=getattr(exc, "detail", "") or "",
        operation=operation,
        segment=segment,
    )


def warning(
    code: ErrorCode,
    user_message: str,
    reason: str = "",
    recommended_action: str = "",
    segment: int | None = None,
) -> OperationError:
    return OperationError(
        code=code,
        user_message=user_message,
        reason=reason,
        recommended_action=recommended_action,
        severity=Severity.WARNING,
        segment=segment,
    )
