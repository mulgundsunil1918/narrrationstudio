"""The project file format, autosave, and crash recovery.

Projects store *references* to generated audio, never the audio itself, so a
five-minute narration project stays a few kilobytes. Every read and write
returns a :class:`Result`; nothing here raises into the UI.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import APP_NAME, PROJECT_SUFFIX, autosave_dir, support_dir
from app.core.models import Segment, SegmentStatus
from app.core.status import ErrorCode, OperationError, Result, capture

logger = logging.getLogger(__name__)

FORMAT_VERSION = 1
FORMAT_MARKER = "narration-studio"
#: Older files carry the original marker; keep reading them.
ACCEPTED_MARKERS = {FORMAT_MARKER, "pediavid"}
RECENTS_LIMIT = 12


@dataclass
class ProjectData:
    """Everything a project remembers between sessions."""

    name: str = "Untitled Project"
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    modified_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    source_srt_path: str = ""
    source_media_path: str = ""
    #: Captions as edited by the user. Timestamps are never rewritten by the app.
    captions: list[dict[str, Any]] = field(default_factory=list)

    engine: str = "kokoro"
    voice: str = "af_heart"
    lang_code: str = "a"
    speed: float = 1.0
    volume: float = 1.0
    voice_preset: str = "Natural"

    narration_mode: str = "natural"
    max_group_ms: int = 60_000
    crossfade_ms: int = 40
    apply_pronunciation: bool = True
    #: How strictly speech is bent to the caption timings.
    pacing: str = "balanced"

    processing_preset: str = "Natural"
    processing_intensity: int = 50

    generated_wav_path: str = ""
    generated_at: str = ""
    generation_state: str = "idle"
    last_error_code: str = ""

    @property
    def duration_ms(self) -> int:
        return max((c.get("end_ms", 0) for c in self.captions), default=0)

    @property
    def caption_count(self) -> int:
        return len(self.captions)

    def touch(self) -> None:
        self.modified_at = datetime.now().isoformat(timespec="seconds")


def segments_to_payload(segments: list[Segment]) -> list[dict[str, Any]]:
    return [
        {
            "uid": s.uid,
            "start_ms": s.start_ms,
            "end_ms": s.end_ms,
            "text": s.text,
            "source_text": s.source_text,
            "status": s.status.value,
        }
        for s in segments
    ]


def payload_to_segments(payload: list[dict[str, Any]]) -> list[Segment]:
    segments: list[Segment] = []
    for item in payload:
        try:
            segments.append(
                Segment(
                    start_ms=int(item["start_ms"]),
                    end_ms=int(item["end_ms"]),
                    text=str(item.get("text", "")),
                    uid=str(item.get("uid")) or Segment(0, 1, "").uid,
                    source_text=str(item.get("source_text", item.get("text", ""))),
                    status=SegmentStatus(item.get("status", "pending")),
                )
            )
        except (KeyError, ValueError, TypeError):
            # Skip an unreadable caption rather than losing the whole project.
            logger.warning("Skipped an unreadable caption while loading a project")
            continue
    return segments


def save(path: Path, data: ProjectData) -> Result[Path]:
    """Write a project atomically."""
    data.touch()
    payload = {"format": FORMAT_MARKER, "version": FORMAT_VERSION, **asdict(data)}

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temporary.replace(path)
    except PermissionError as exc:
        return Result.fail(
            OperationError(
                ErrorCode.FILE_PERMISSION_DENIED,
                "This project could not be saved because the folder is not writable.",
                reason=str(exc),
                recommended_action="Use File ▸ Save As and choose a different folder.",
                operation="project_save",
                details=str(exc),
            )
        )
    except OSError as exc:
        code = (
            ErrorCode.DISK_SPACE_LOW
            if getattr(exc, "errno", None) == 28
            else ErrorCode.PROJECT_SAVE_FAILED
        )
        return Result.fail(
            OperationError(
                code,
                "This project could not be saved.",
                reason=str(exc),
                recommended_action=(
                    "Free up disk space and try again."
                    if code is ErrorCode.DISK_SPACE_LOW
                    else "Try saving to a different location."
                ),
                operation="project_save",
                details=str(exc),
            )
        )
    except Exception as exc:
        return Result.fail(
            capture(
                exc,
                ErrorCode.PROJECT_SAVE_FAILED,
                user_message="This project could not be saved.",
                recommended_action="Try saving to a different location.",
                operation="project_save",
            )
        )

    remember_recent(path)
    return Result.ok(path)


def load(path: Path) -> Result[ProjectData]:
    """Read a project, explaining precisely what is wrong if it cannot be read."""
    if not path.exists():
        return Result.fail(
            OperationError(
                ErrorCode.FILE_NOT_FOUND,
                f"“{path.name}” could not be found.",
                reason="The file may have been moved, renamed or deleted.",
                recommended_action="Locate the project file, or open a different one.",
                operation="project_load",
            )
        )

    try:
        raw = path.read_text(encoding="utf-8")
    except PermissionError as exc:
        return Result.fail(
            OperationError(
                ErrorCode.FILE_PERMISSION_DENIED,
                f"“{path.name}” could not be opened.",
                reason="macOS denied read access to this file.",
                recommended_action="Check the file's permissions and try again.",
                operation="project_load",
                details=str(exc),
            )
        )
    except OSError as exc:
        return Result.fail(
            capture(
                exc,
                ErrorCode.PROJECT_LOAD_FAILED,
                user_message=f"“{path.name}” could not be read.",
                operation="project_load",
            )
        )

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        return Result.fail(
            OperationError(
                ErrorCode.PROJECT_LOAD_FAILED,
                f"“{path.name}” is damaged and cannot be opened.",
                reason=f"The file is not valid project data (line {exc.lineno}).",
                recommended_action=(
                    "Open a different project, or re-import the original SRT to "
                    "start again."
                ),
                operation="project_load",
                details=str(exc),
            )
        )

    if not isinstance(payload, dict) or payload.get("format") not in ACCEPTED_MARKERS:
        return Result.fail(
            OperationError(
                ErrorCode.PROJECT_LOAD_FAILED,
                f"“{path.name}” is not a {APP_NAME} project.",
                reason="The file does not carry the expected project marker.",
                recommended_action=f"Choose a file ending in {PROJECT_SUFFIX}.",
                operation="project_load",
            )
        )

    known = {f.name for f in ProjectData.__dataclass_fields__.values()}
    data = ProjectData(**{k: v for k, v in payload.items() if k in known})
    remember_recent(path)
    return Result.ok(data)


# -- recents -------------------------------------------------------------


def recents_path() -> Path:
    return support_dir() / "recent-projects.json"


def recent_projects() -> list[Path]:
    path = recents_path()
    if not path.exists():
        return []
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(entries, list):
        return []
    return [Path(entry) for entry in entries if isinstance(entry, str) and Path(entry).exists()]


def remember_recent(path: Path) -> None:
    entries = [str(path.resolve())]
    entries += [str(p) for p in recent_projects() if p.resolve() != path.resolve()]
    try:
        recents_path().write_text(
            json.dumps(entries[:RECENTS_LIMIT], indent=2), encoding="utf-8"
        )
    except OSError:
        logger.warning("Could not update the recent-projects list", exc_info=True)


def forget_recent(path: Path) -> None:
    entries = [str(p) for p in recent_projects() if p.resolve() != path.resolve()]
    try:
        recents_path().write_text(json.dumps(entries, indent=2), encoding="utf-8")
    except OSError:
        logger.warning("Could not update the recent-projects list", exc_info=True)


# -- autosave and recovery ----------------------------------------------


def autosave_path() -> Path:
    return autosave_dir() / f"recovery{PROJECT_SUFFIX}"


def autosave(data: ProjectData) -> Result[Path]:
    return save(autosave_path(), data)


def pending_recovery() -> Path | None:
    """An autosave newer than its project file means the app did not exit cleanly."""
    path = autosave_path()
    return path if path.exists() else None


def clear_recovery() -> None:
    path = autosave_path()
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning("Could not clear the recovery file", exc_info=True)


def duplicate(path: Path) -> Result[Path]:
    """Copy a project to "<name> copy" beside the original."""
    if not path.exists():
        return Result.fail(
            OperationError(
                ErrorCode.FILE_NOT_FOUND,
                f"“{path.name}” could not be found, so it cannot be duplicated.",
                recommended_action="Refresh the project list.",
                operation="project_duplicate",
            )
        )
    target = path.with_name(f"{path.stem} copy{PROJECT_SUFFIX}")
    index = 2
    while target.exists():
        target = path.with_name(f"{path.stem} copy {index}{PROJECT_SUFFIX}")
        index += 1
    try:
        shutil.copy2(path, target)
    except OSError as exc:
        return Result.fail(
            capture(
                exc,
                ErrorCode.PROJECT_SAVE_FAILED,
                user_message=f"“{path.name}” could not be duplicated.",
                recommended_action="Check that the folder is writable.",
                operation="project_duplicate",
            )
        )
    remember_recent(target)
    return Result.ok(target)


def delete(path: Path) -> Result[Path]:
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        return Result.fail(
            capture(
                exc,
                ErrorCode.PROJECT_SAVE_FAILED,
                user_message=f"“{path.name}” could not be deleted.",
                recommended_action="Check the file's permissions.",
                operation="project_delete",
            )
        )
    forget_recent(path)
    return Result.ok(path)
