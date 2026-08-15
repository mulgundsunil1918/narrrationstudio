"""The end-to-end generation pipeline.

    SRT → validate → group → TTS per group → fit to window → assemble → WAV

Shared by the command line and the app so both behave identically. Progress is
reported through a callback rather than printed, so the UI can stay responsive
and the CLI can print.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Sequence

import numpy as np

from app.audio.assemble import (
    DEFAULT_CROSSFADE_MS,
    DEFAULT_PEAK,
    DEFAULT_SAMPLE_RATE,
    AssemblyReport,
    PlacedGroup,
    assemble,
    fit_audio,
    write_mp3,
    write_wav,
)
from app.audio.timing import FitOptions, FitPlan
from app.cache.store import AudioCache, CacheKey
from app.config import audio_cache_dir
from app.core.errors import AudioError, StudioError
from app.core.models import Segment
from app.core.status import ErrorCode, OperationError, OperationState, capture
from app.core.validation import validate
from app.narration.grouping import GroupingOptions, build_plan
from app.narration.groups import GroupWindow, NarrationMode, NarrationPlan, SpeedSafety
from app.srt.parser import load as load_subtitles
from app.tts import pronunciation
from app.tts.base import EngineUnavailable, GenerationRequest
from app.tts.registry import engine as get_engine

logger = logging.getLogger(__name__)

OUTPUT_SUFFIX = "_Narration"


@dataclass
class GenerationSettings:
    """Everything that determines the output. Serialisable into a project file."""

    engine: str = "kokoro"
    voice: str = "af_heart"
    lang_code: str = "a"
    speed: float = 1.0
    sample_rate: int = DEFAULT_SAMPLE_RATE
    mode: NarrationMode = NarrationMode.NATURAL
    grouping: GroupingOptions = field(default_factory=GroupingOptions)
    fit: FitOptions = field(default_factory=FitOptions)
    crossfade_ms: int = DEFAULT_CROSSFADE_MS
    peak: float = DEFAULT_PEAK
    apply_pronunciation: bool = True
    use_cache: bool = True
    #: Generate only the groups starting before this point. A *testing* limit
    #: on how much is rendered -- it never changes the project's own length.
    preview_until_ms: int | None = None


@dataclass
class GroupProgress:
    index: int
    total: int
    start_ms: int
    end_ms: int
    target_ms: int
    text: str
    generated_ms: int = 0
    final_ms: int = 0
    speed_factor: float = 1.0
    safety: SpeedSafety = SpeedSafety.SAFE
    from_cache: bool = False
    seconds_taken: float = 0.0
    message: str = ""
    failed: bool = False
    error: OperationError | None = None
    #: Fired before synthesis so the UI can show what is being worked on rather
    #: than only what has finished.
    starting: bool = False


ProgressCallback = Callable[[GroupProgress], None]


class Cancelled(Exception):
    """Raised internally when the user cancels; converted to a clean result."""


class CancellationToken:
    """Cooperative cancellation checked between narration groups.

    Cancelling must actually stop the work rather than hide the screen, but a
    group already inside the model cannot be interrupted safely -- so the token
    is checked at every group boundary and the partial timeline is kept.
    """

    def __init__(self) -> None:
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def raise_if_cancelled(self) -> None:
        if self._cancelled:
            raise Cancelled()


@dataclass
class GenerationOutcome:
    audio: np.ndarray
    sample_rate: int
    timeline_ms: int
    plan: NarrationPlan
    fit_plans: list[FitPlan]
    report: AssemblyReport
    cache_hits: int
    cache_misses: int
    seconds_taken: float
    warnings: list[str] = field(default_factory=list)
    #: Groups that failed, kept so the user can retry just those (§7).
    failures: list[OperationError] = field(default_factory=list)
    completed_groups: int = 0
    cancelled: bool = False

    @property
    def duration_ms(self) -> int:
        return int(round(len(self.audio) / self.sample_rate * 1000))

    @property
    def state(self) -> OperationState:
        if self.cancelled:
            return OperationState.CANCELLED
        if self.failures:
            return OperationState.ERROR
        if self.warnings:
            return OperationState.WARNING
        return OperationState.COMPLETED

    @property
    def failed_segments(self) -> list[int]:
        return [f.segment for f in self.failures if f.segment is not None]

    @property
    def flagged(self) -> list[int]:
        return [
            i
            for i, plan in enumerate(self.fit_plans)
            if plan.safety is not SpeedSafety.SAFE
        ]


def derive_output_path(source: Path, destination: Path | None = None) -> Path:
    """``Tutorial_01.srt`` → ``Tutorial_01_Narration.wav`` beside the source."""
    if destination is not None:
        if destination.is_dir():
            return destination / f"{source.stem}{OUTPUT_SUFFIX}.wav"
        return destination
    return source.with_name(f"{source.stem}{OUTPUT_SUFFIX}.wav")


def generate(
    segments: Sequence[Segment],
    settings: GenerationSettings | None = None,
    on_progress: ProgressCallback | None = None,
    cache: AudioCache | None = None,
    token: CancellationToken | None = None,
    only_groups: Sequence[int] | None = None,
) -> GenerationOutcome:
    """Run the full pipeline over ``segments`` and return the finished timeline.

    A failing group never discards the rest: it is recorded in ``failures``, its
    window is left silent, and every other group is still placed. ``only_groups``
    restricts work to specific indices so a single failed segment can be retried
    without regenerating the project.
    """
    settings = settings or GenerationSettings()
    started = time.monotonic()
    warnings: list[str] = []
    failures: list[OperationError] = []
    token = token or CancellationToken()
    cancelled = False

    # A bool here is never a group filter — it is a Qt `checked` flag that fell
    # through a signal connection into an optional parameter. It once killed
    # every generation started from a button, so it is neutralised at the
    # boundary rather than trusted anywhere below.
    if isinstance(only_groups, bool):
        logger.warning("only_groups received a bool (%s); treating as no filter", only_groups)
        only_groups = None

    if not segments:
        raise StudioError(
            "There are no subtitles to generate from.",
            suggestion="Import an SRT file first.",
        )

    report = validate(segments)
    if report.errors:
        first = report.errors[0]
        raise StudioError(
            f"The subtitle timeline has {len(report.errors)} problem(s) that would "
            f"produce incorrect audio. First: {first.message}",
            suggestion=first.suggestion,
        )

    backend = get_engine(settings.engine)
    available, reason = backend.is_available()
    if not available:
        raise EngineUnavailable(
            reason, suggestion="Run ./setup.sh to install the local speech engine."
        )

    narration = build_plan(segments, settings.mode, settings.grouping)
    window = GroupWindow(segments)
    cache = cache or AudioCache(audio_cache_dir())
    entries = pronunciation.load(_pronunciation_path()) if settings.apply_pronunciation else []

    # The timeline is always the full SRT length. A preview limits how many
    # groups are *rendered*, never how long the project is.
    timeline_ms = max(segment.end_ms for segment in segments)

    placed: list[PlacedGroup] = []
    fit_plans: list[FitPlan] = []
    total = len(narration)

    for index, group in enumerate(narration):
        group_start = window.start_ms(group)
        group_end = window.end_ms(group)
        target_ms = window.target_ms(group)
        caption_text = window.narration_text(group)

        if settings.preview_until_ms is not None and group_start >= settings.preview_until_ms:
            continue
        if only_groups is not None and index not in only_groups:
            continue

        if token.cancelled:
            cancelled = True
            logger.info("Generation cancelled by the user before group %d", index + 1)
            break

        progress = GroupProgress(
            index=index,
            total=total,
            start_ms=group_start,
            end_ms=group_end,
            target_ms=target_ms,
            text=caption_text,
        )
        if on_progress:
            on_progress(replace(progress, starting=True))

        spoken_text = (
            pronunciation.apply(caption_text, entries)
            if settings.apply_pronunciation
            else caption_text
        )

        key = CacheKey(
            engine=settings.engine,
            model="kokoro-v1_0",
            voice=settings.voice,
            lang_code=settings.lang_code,
            text=spoken_text,
            speed=settings.speed,
            sample_rate=settings.sample_rate,
        )

        group_started = time.monotonic()
        try:
            cached = cache.get(key) if settings.use_cache else None
            if cached is not None:
                audio, _ = cached
                progress.from_cache = True
            else:
                result = backend.generate(
                    GenerationRequest(
                        text=spoken_text,
                        voice=settings.voice,
                        lang_code=settings.lang_code,
                        speed=settings.speed,
                        sample_rate=settings.sample_rate,
                    )
                )
                audio = result.audio
                if len(audio) == 0 and spoken_text.strip():
                    raise EmptyAudio(
                        "The voice model returned no audio for this section."
                    )
                if settings.use_cache and len(audio):
                    cache.put(key, audio, result.sample_rate)

            fitted, fit_plan = fit_audio(
                audio, target_ms, settings.sample_rate, settings.fit
            )
        except Exception as exc:
            # One bad group must not cost the other twenty. Record it, leave its
            # window silent, and carry on so the rest of the project survives.
            error = _describe_group_failure(exc, index, settings.voice)
            failures.append(error)
            logger.error(
                "TTS_GENERATION segment %d ERROR %s", index + 1, error.code.value
            )
            progress.failed = True
            progress.error = error
            progress.seconds_taken = time.monotonic() - group_started
            if on_progress:
                on_progress(progress)
            continue

        fit_plans.append(fit_plan)

        if fit_plan.needs_confirmation:
            warnings.append(
                f"Group {index + 1} needs {fit_plan.speed_factor:.2f}× compression "
                f"to fit {target_ms / 1000:.1f}s — it was left at that speed and "
                "will sound rushed. Shorten the text or lengthen the window."
            )
        elif fit_plan.message:
            warnings.append(f"Group {index + 1}: {fit_plan.message}")

        placed.append(
            PlacedGroup(
                index=index,
                start_ms=group_start,
                end_ms=group_end,
                audio=fitted,
                plan=fit_plan,
                label=f"Group {index + 1}",
            )
        )

        progress.generated_ms = fit_plan.generated_ms
        progress.final_ms = fit_plan.final_ms
        progress.speed_factor = fit_plan.speed_factor
        progress.safety = fit_plan.safety
        progress.seconds_taken = time.monotonic() - group_started
        progress.message = fit_plan.message
        if on_progress:
            on_progress(progress)

    audio, assembly = assemble(
        placed,
        timeline_ms=timeline_ms,
        sample_rate=settings.sample_rate,
        peak=settings.peak,
        crossfade_ms=settings.crossfade_ms,
    )
    warnings.extend(assembly.issues)

    logger.info(
        "GENERATION finished: %d/%d groups, %d failed, %.1fs",
        len(placed), total, len(failures), time.monotonic() - started,
    )

    return GenerationOutcome(
        audio=audio,
        sample_rate=settings.sample_rate,
        timeline_ms=timeline_ms,
        plan=narration,
        fit_plans=fit_plans,
        report=assembly,
        cache_hits=cache.hits,
        cache_misses=cache.misses,
        seconds_taken=time.monotonic() - started,
        warnings=warnings,
        failures=failures,
        completed_groups=len(placed),
        cancelled=cancelled,
    )


class EmptyAudio(Exception):
    """The engine returned a zero-length buffer for non-empty text."""


def _describe_group_failure(
    exc: Exception, index: int, voice: str
) -> OperationError:
    """Convert a per-group exception into something the user can act on."""
    segment = index + 1

    if isinstance(exc, EmptyAudio):
        return OperationError(
            code=ErrorCode.TTS_EMPTY_AUDIO,
            user_message=f"Unable to generate narration for section {segment}.",
            reason=f"The voice “{voice}” returned no audio for this text.",
            recommended_action="Try another voice, or retry this section.",
            details=str(exc),
            operation="tts_generation",
            segment=segment,
        )
    if isinstance(exc, EngineUnavailable):
        return OperationError(
            code=ErrorCode.VOICE_MODEL_LOAD_FAILED,
            user_message=f"Unable to generate narration for section {segment}.",
            reason=getattr(exc, "message", str(exc)),
            recommended_action=getattr(exc, "suggestion", "Try another voice."),
            details=getattr(exc, "detail", "") or str(exc),
            operation="tts_generation",
            segment=segment,
        )
    if isinstance(exc, AudioError):
        return OperationError(
            code=ErrorCode.AUDIO_PROCESSING_FAILED,
            user_message=f"Section {segment} could not be fitted to its timing.",
            reason=getattr(exc, "message", str(exc)),
            recommended_action=getattr(exc, "suggestion", "Retry this section."),
            details=getattr(exc, "detail", "") or str(exc),
            operation="audio_fit",
            segment=segment,
        )
    return capture(
        exc,
        ErrorCode.TTS_GENERATION_FAILED,
        user_message=f"Unable to generate narration for section {segment}.",
        recommended_action="Retry this section, or choose a different voice.",
        operation="tts_generation",
        segment=segment,
    )


def generate_from_file(
    source: Path,
    destination: Path | None = None,
    settings: GenerationSettings | None = None,
    on_progress: ProgressCallback | None = None,
    also_mp3: bool = False,
) -> tuple[Path, GenerationOutcome]:
    """Import ``source``, generate, and write the WAV. Returns its path."""
    parsed = load_subtitles(source)
    for warning in parsed.warnings:
        logger.warning("%s: %s", source.name, warning)

    outcome = generate(parsed.segments, settings, on_progress)
    output = derive_output_path(source, destination)
    write_wav(output, outcome.audio, outcome.sample_rate)

    if also_mp3:
        write_mp3(output.with_suffix(".mp3"), output)
    return output, outcome


def _pronunciation_path() -> Path:
    from app.config import support_dir

    return support_dir() / "pronunciation.json"
