"""User-facing errors (§27).

Every failure the UI shows is one of these: a short headline, a plain-language
reason, and a concrete next step. The original exception is kept on ``.detail``
for the "View Technical Details" disclosure, but never shown by default.
"""

from __future__ import annotations

import traceback


class StudioError(Exception):
    """Base class for errors that are safe and useful to show the user."""

    headline = "Something went wrong"

    def __init__(
        self,
        message: str,
        *,
        reason: str = "",
        suggestion: str = "",
        detail: str = "",
        cause: BaseException | None = None,
        context: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        #: Why it happened, in plain language. Shown under "Why this happened".
        self.reason = reason
        self.suggestion = suggestion
        self.context = context or {}
        if cause is not None and not detail:
            detail = "".join(
                traceback.format_exception(type(cause), cause, cause.__traceback__)
            )
        self.detail = detail

    def __str__(self) -> str:
        return self.message


class FileFormatError(StudioError):
    """An imported file could not be understood."""

    headline = "Could not read this file"


class UnsupportedFileError(StudioError):
    """The file type is not one this app handles."""

    headline = "Unsupported file type"


class DependencyError(StudioError):
    """A required external tool or package is missing (§38)."""

    headline = "A required component is missing"


class GenerationError(StudioError):
    """TTS generation failed for a segment."""

    headline = "TTS generation failed"


class AudioError(StudioError):
    """Audio processing or export failed."""

    headline = "Audio processing failed"


class ProjectError(StudioError):
    """A project file could not be read or written."""

    headline = "Project file problem"


#: Historical name, kept so older imports keep working.
PediAidError = StudioError
