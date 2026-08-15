"""Application state.

One object owns the project, the settings and the generation state machine.
Screens read from it and listen to its signals; they never talk to each other
directly, and they never hold their own copy of the truth.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PySide6.QtCore import QObject, QTimer, Signal

from app.audio.timing import FitOptions
from app.config import DEFAULT_VOICE, PROJECT_SUFFIX, Settings
from app.core.document import SubtitleDocument
from app.core.models import Segment
from app.core.status import ErrorCode, OperationError, OperationState, Result
from app.narration.grouping import GroupingOptions, build_plan
from app.narration.groups import GroupWindow, NarrationMode, NarrationPlan
from app.pipeline import GenerationOutcome, GenerationSettings
from app.projects import store
from app.projects.store import ProjectData

logger = logging.getLogger(__name__)

AUTOSAVE_INTERVAL_MS = 20_000


@dataclass
class VoiceSettings:
    engine: str = "kokoro"
    voice: str = DEFAULT_VOICE
    lang_code: str = "a"
    speed: float = 1.0
    volume: float = 1.0
    preset: str = "Natural"


@dataclass
class NarrationSettings:
    mode: NarrationMode = NarrationMode.NATURAL
    max_group_ms: int = 60_000
    crossfade_ms: int = 40
    apply_pronunciation: bool = True
    fit: FitOptions = field(default_factory=FitOptions)


class AppState(QObject):
    """The single source of truth for the running application."""

    # -- project ---------------------------------------------------------
    project_changed = Signal()          # captions, name or path changed
    project_saved = Signal(object)      # Path
    dirty_changed = Signal(bool)

    # -- settings --------------------------------------------------------
    voice_changed = Signal()
    narration_changed = Signal()
    advanced_changed = Signal(bool)

    # -- generation state machine ----------------------------------------
    state_changed = Signal(object)      # OperationState
    generation_finished = Signal(object)  # GenerationOutcome
    error_raised = Signal(object)       # OperationError
    notify = Signal(str, str)           # (message, kind)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.settings = Settings.load()
        self.document = SubtitleDocument()
        self.project = ProjectData()
        self.project_path: Path | None = None
        self.media_path: Path | None = None

        self.voice = VoiceSettings(
            engine=self.settings.engine,
            voice=self.settings.voice or DEFAULT_VOICE,
            lang_code=self.settings.lang_code,
        )
        self.narration = NarrationSettings()

        self.outcome: GenerationOutcome | None = None
        self.generated_audio: np.ndarray | None = None
        self.generated_path: Path | None = None

        self._state = OperationState.IDLE
        self._advanced = False
        self._plan: NarrationPlan | None = None

        self.document.add_listener(self._on_document_changed)

        self._autosave = QTimer(self)
        self._autosave.setInterval(AUTOSAVE_INTERVAL_MS)
        self._autosave.timeout.connect(self.autosave)

    # -- state machine ---------------------------------------------------

    @property
    def state(self) -> OperationState:
        return self._state

    def set_state(self, state: OperationState) -> None:
        if state is self._state:
            return
        logger.info("state: %s -> %s", self._state.value, state.value)
        self._state = state
        self.state_changed.emit(state)

    @property
    def is_busy(self) -> bool:
        return self._state.is_busy

    # -- advanced mode ---------------------------------------------------

    @property
    def advanced(self) -> bool:
        return self._advanced

    def set_advanced(self, enabled: bool) -> None:
        if enabled != self._advanced:
            self._advanced = enabled
            self.advanced_changed.emit(enabled)

    # -- errors ----------------------------------------------------------

    def raise_error(self, error: OperationError) -> None:
        """Every failure funnels through here so none can be lost."""
        logger.error("%s: %s", error.code.value, error.user_message)
        self.set_state(OperationState.ERROR)
        self.error_raised.emit(error)

    def report(self, message: str, kind: str = "info") -> None:
        self.notify.emit(message, kind)

    # -- captions --------------------------------------------------------

    @property
    def segments(self) -> list[Segment]:
        return self.document.segments

    @property
    def has_captions(self) -> bool:
        return len(self.document) > 0

    @property
    def timeline_ms(self) -> int:
        return self.document.timeline_end_ms

    def load_segments(self, segments: list[Segment], source: Path | None, name: str = "") -> None:
        self.document.load(segments, source)
        self.project.captions = store.segments_to_payload(segments)
        if source is not None:
            self.project.source_srt_path = str(source)
        # Captions transcribed from a video have no source file, but they still
        # deserve the video's name rather than an untitled project.
        if name or source is not None:
            self.project.name = name or source.stem.replace("_", " ")
        self._plan = None
        self.outcome = None
        self.generated_audio = None
        self.set_state(OperationState.IDLE)
        self.project_changed.emit()
        self._autosave.start()

    def _on_document_changed(self) -> None:
        self.project.captions = store.segments_to_payload(self.document.segments)
        self._plan = None
        # Editing captions invalidates any generated audio.
        if self.outcome is not None:
            self.outcome = None
            self.generated_audio = None
            self.set_state(OperationState.IDLE)
        self.dirty_changed.emit(self.document.is_dirty)
        self.project_changed.emit()

    # -- narration plan --------------------------------------------------

    def grouping_options(self) -> GroupingOptions:
        return GroupingOptions(max_group_ms=self.narration.max_group_ms)

    def plan(self) -> NarrationPlan:
        """The narration plan, rebuilt lazily whenever captions change."""
        if self._plan is None:
            self._plan = build_plan(
                self.document.segments, self.narration.mode, self.grouping_options()
            )
        return self._plan

    def window(self) -> GroupWindow:
        return GroupWindow(self.document.segments)

    def invalidate_plan(self) -> None:
        self._plan = None
        self.narration_changed.emit()

    def generation_settings(self) -> GenerationSettings:
        return GenerationSettings(
            engine=self.voice.engine,
            voice=self.voice.voice,
            lang_code=self.voice.lang_code,
            speed=self.voice.speed,
            mode=self.narration.mode,
            grouping=self.grouping_options(),
            fit=self.narration.fit,
            crossfade_ms=self.narration.crossfade_ms,
            apply_pronunciation=self.narration.apply_pronunciation,
        )

    # -- voice -----------------------------------------------------------

    def set_voice(self, voice_id: str, lang_code: str = "a") -> None:
        self.voice.voice = voice_id
        self.voice.lang_code = lang_code
        self.settings.voice = voice_id
        self.settings.lang_code = lang_code
        recents = [voice_id] + [v for v in self.settings.recent_voices if v != voice_id]
        self.settings.recent_voices = recents[:8]
        self.settings.save()
        self.voice_changed.emit()

    def toggle_favourite(self, voice_id: str) -> bool:
        favourites = list(self.settings.favourite_voices)
        if voice_id in favourites:
            favourites.remove(voice_id)
            added = False
        else:
            favourites.append(voice_id)
            added = True
        self.settings.favourite_voices = favourites
        self.settings.save()
        self.voice_changed.emit()
        return added

    def is_favourite(self, voice_id: str) -> bool:
        return voice_id in self.settings.favourite_voices

    # -- generation results ----------------------------------------------

    def set_outcome(self, outcome: GenerationOutcome) -> None:
        self.outcome = outcome
        self.generated_audio = outcome.audio
        self.project.generation_state = outcome.state.value
        self.set_state(outcome.state)
        self.generation_finished.emit(outcome)

    # -- persistence -----------------------------------------------------

    def _sync_project(self) -> None:
        self.project.captions = store.segments_to_payload(self.document.segments)
        self.project.engine = self.voice.engine
        self.project.voice = self.voice.voice
        self.project.lang_code = self.voice.lang_code
        self.project.speed = self.voice.speed
        self.project.volume = self.voice.volume
        self.project.voice_preset = self.voice.preset
        self.project.narration_mode = self.narration.mode.value
        self.project.max_group_ms = self.narration.max_group_ms
        self.project.crossfade_ms = self.narration.crossfade_ms
        self.project.apply_pronunciation = self.narration.apply_pronunciation
        if self.generated_path:
            self.project.generated_wav_path = str(self.generated_path)
        if self.media_path:
            self.project.source_media_path = str(self.media_path)

    def save(self, path: Path | None = None) -> Result[Path]:
        target = path or self.project_path
        if target is None:
            return Result.fail(
                OperationError(
                    ErrorCode.PROJECT_SAVE_FAILED,
                    "This project has not been given a location yet.",
                    reason="No file name has been chosen.",
                    recommended_action="Use File ▸ Save As to choose where to save it.",
                    operation="project_save",
                )
            )
        if target.suffix != PROJECT_SUFFIX:
            target = target.with_suffix(PROJECT_SUFFIX)

        self._sync_project()
        result = store.save(target, self.project)
        if result.success:
            self.project_path = target
            self.document.mark_saved()
            self.dirty_changed.emit(False)
            self.project_saved.emit(target)
        return result

    def autosave(self) -> None:
        """Best-effort recovery snapshot. Reports a failure once, then backs off."""
        if not self.has_captions:
            return
        self._sync_project()
        result = store.autosave(self.project)
        if not result.success and result.error:
            self._autosave.stop()
            self.raise_error(result.error)

    def open_project(self, path: Path) -> Result[ProjectData]:
        result = store.load(path)
        if not result.success:
            return result

        data = result.unwrap()
        segments = store.payload_to_segments(data.captions)
        if not segments:
            return Result.fail(
                OperationError(
                    ErrorCode.PROJECT_LOAD_FAILED,
                    f"“{path.name}” opened but contains no subtitles.",
                    reason="The project's caption list is empty or unreadable.",
                    recommended_action="Re-import the original SRT file.",
                    operation="project_load",
                )
            )

        self.project = data
        self.project_path = path
        self.document.load(segments, Path(data.source_srt_path) if data.source_srt_path else None)
        self.voice = VoiceSettings(
            engine=data.engine,
            voice=data.voice,
            lang_code=data.lang_code,
            speed=data.speed,
            volume=data.volume,
            preset=data.voice_preset,
        )
        try:
            mode = NarrationMode(data.narration_mode)
        except ValueError:
            mode = NarrationMode.NATURAL
        self.narration = NarrationSettings(
            mode=mode,
            max_group_ms=data.max_group_ms,
            crossfade_ms=data.crossfade_ms,
            apply_pronunciation=data.apply_pronunciation,
        )
        self.media_path = Path(data.source_media_path) if data.source_media_path else None
        self._plan = None
        self.outcome = None
        self.generated_audio = None
        self.set_state(OperationState.IDLE)
        self.project_changed.emit()
        self.voice_changed.emit()
        self.narration_changed.emit()
        self._autosave.start()
        return result

    def new_project(self) -> None:
        self.document.load([])
        self.project = ProjectData()
        self.project_path = None
        self.media_path = None
        self.outcome = None
        self.generated_audio = None
        self.generated_path = None
        self._plan = None
        self.set_state(OperationState.IDLE)
        self.project_changed.emit()
