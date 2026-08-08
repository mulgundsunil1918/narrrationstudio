"""Assembling narration groups onto the master timeline.

The rule that makes drift impossible, stated once and enforced here:

    Every group is written at ``ms_to_samples(group_start)``.

The position of a group is never derived from where the previous group's audio
ended. If group 3 renders 200 ms short, group 4 still begins exactly where the
SRT says it does — the shortfall becomes silence, not a shift.
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from app.audio.timing import FitOptions, FitPlan, atempo_filter, plan_fit
from app.core.errors import AudioError
from app.core.timecode import ms_to_samples
from app.narration.groups import SpeedSafety

logger = logging.getLogger(__name__)

DEFAULT_SAMPLE_RATE = 24_000
#: Peak the proof-of-concept normalised to; preserved so output levels match.
DEFAULT_PEAK = 0.92
#: Micro-crossfade between adjacent groups. Long enough to kill a click, far too
#: short to overlap words.
DEFAULT_CROSSFADE_MS = 40
MAX_CROSSFADE_MS = 80


@dataclass
class PlacedGroup:
    """One group's audio, already fitted, with its exact position."""

    index: int
    start_ms: int
    end_ms: int
    audio: np.ndarray
    plan: FitPlan
    label: str = ""

    @property
    def start_sample(self) -> int:
        return ms_to_samples(self.start_ms, DEFAULT_SAMPLE_RATE)


@dataclass
class AssemblyReport:
    timeline_ms: int
    sample_rate: int
    group_count: int
    peak_before_normalise: float
    gain_applied: float
    crossfades_applied: int
    issues: list[str] = field(default_factory=list)

    @property
    def duration_ms(self) -> int:
        return self.timeline_ms


def fit_audio(
    audio: np.ndarray,
    target_ms: int,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    options: FitOptions | None = None,
    ffmpeg: str = "ffmpeg",
) -> tuple[np.ndarray, FitPlan]:
    """Make ``audio`` exactly ``target_ms`` long, without ever truncating speech.

    Speeds up or slows down with ffmpeg's pitch-preserving ``atempo``, then pads
    with silence at the end if anything is left over.
    """
    generated_ms = int(round(len(audio) / sample_rate * 1000))
    plan = plan_fit(generated_ms, target_ms, options)
    target_samples = ms_to_samples(target_ms, sample_rate)

    if target_samples <= 0:
        return np.zeros(0, dtype=np.float32), plan

    if len(audio) == 0:
        return np.zeros(target_samples, dtype=np.float32), plan

    working = audio.astype(np.float32, copy=False)
    if abs(plan.speed_factor - 1.0) > 1e-6:
        working = _apply_atempo(working, plan.speed_factor, sample_rate, ffmpeg)

    # Silence goes only at the END of the group, never between its captions.
    if len(working) < target_samples:
        working = np.pad(working, (0, target_samples - len(working)))
    elif len(working) > target_samples:
        # atempo lands within a few samples; trimming that is not truncating speech.
        working = working[:target_samples]

    return working.astype(np.float32, copy=False), plan


def _apply_atempo(
    audio: np.ndarray, factor: float, sample_rate: int, ffmpeg: str
) -> np.ndarray:
    """Pitch-preserving time-stretch via ffmpeg, using an argument array."""
    import soundfile as sf

    with tempfile.TemporaryDirectory(prefix="pediaid_fit_") as directory:
        source = Path(directory) / "in.wav"
        destination = Path(directory) / "out.wav"
        sf.write(str(source), audio, sample_rate)

        command = [
            ffmpeg, "-y", "-loglevel", "error",
            "-i", str(source),
            "-filter:a", atempo_filter(factor),
            "-ar", str(sample_rate), "-ac", "1",
            str(destination),
        ]
        try:
            subprocess.run(command, check=True, capture_output=True)
        except FileNotFoundError as exc:
            raise AudioError(
                "FFmpeg was not found, so speech cannot be fitted to its window.",
                suggestion="Install it with: brew install ffmpeg",
                cause=exc,
            ) from exc
        except subprocess.CalledProcessError as exc:
            raise AudioError(
                "FFmpeg could not adjust the speed of this narration group.",
                suggestion="Try regenerating the group, or use a different voice.",
                detail=exc.stderr.decode("utf-8", "replace")[:2000],
                cause=exc,
            ) from exc

        fitted, _ = sf.read(str(destination), dtype="float32", always_2d=False)

    if fitted.ndim > 1:
        fitted = fitted.mean(axis=1)
    return fitted.astype(np.float32, copy=False)


def assemble(
    placed: list[PlacedGroup],
    timeline_ms: int,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    peak: float = DEFAULT_PEAK,
    crossfade_ms: int = DEFAULT_CROSSFADE_MS,
) -> tuple[np.ndarray, AssemblyReport]:
    """Write every group onto one zero-filled timeline of the SRT's length."""
    total_samples = ms_to_samples(timeline_ms, sample_rate)
    timeline = np.zeros(max(0, total_samples), dtype=np.float32)
    issues: list[str] = []
    crossfades = 0

    crossfade_ms = max(0, min(int(crossfade_ms), MAX_CROSSFADE_MS))
    fade_samples = ms_to_samples(crossfade_ms, sample_rate) if crossfade_ms else 0

    for position, group in enumerate(placed):
        start = group.start_sample
        end = start + len(group.audio)

        if start < 0:
            issues.append(f"Group {group.index + 1} starts before the timeline.")
            continue
        if end > len(timeline):
            # Clamp rather than grow: the SRT defines the length, not the audio.
            overflow_ms = int(round((end - len(timeline)) / sample_rate * 1000))
            issues.append(
                f"Group {group.index + 1} ran {overflow_ms} ms past the end of the "
                "timeline and was clipped to it."
            )
            end = len(timeline)

        chunk = group.audio[: end - start]
        if len(chunk) == 0:
            continue

        # Two independently rendered buffers meeting at a group join leave a
        # step discontinuity, heard as a click. A short equal-power crossfade
        # over the *preceding* group's tail removes it: the incoming group
        # leads in by at most 40 ms, which is far below the threshold of
        # perception and cannot overlap a word. Total length is unchanged, and
        # no caption timestamp moves.
        overlap = 0
        if (
            fade_samples
            and position > 0
            and start >= fade_samples
            and len(chunk) > fade_samples
            and np.any(timeline[start - fade_samples : start])
        ):
            overlap = fade_samples
            ramp = np.linspace(0.0, 1.0, overlap, dtype=np.float32)
            region = slice(start - overlap, start)
            timeline[region] = (
                timeline[region] * np.sqrt(1.0 - ramp) + chunk[:overlap] * np.sqrt(ramp)
            )
            crossfades += 1

        # The remainder is written at the group's exact start sample.
        tail = chunk[overlap:]
        timeline[start : start + len(tail)] = tail

    peak_value = float(np.max(np.abs(timeline))) if len(timeline) else 0.0
    gain = 1.0
    if peak_value > 0:
        gain = min(peak / peak_value, 1.0) if peak_value > peak else 1.0
        # Match the proof-of-concept: normalise down to the headroom target, and
        # never amplify quiet material up into it.
        timeline = timeline * gain

    return timeline, AssemblyReport(
        timeline_ms=timeline_ms,
        sample_rate=sample_rate,
        group_count=len(placed),
        peak_before_normalise=peak_value,
        gain_applied=gain,
        crossfades_applied=crossfades,
        issues=issues,
    )


def write_wav(
    path: Path, audio: np.ndarray, sample_rate: int = DEFAULT_SAMPLE_RATE
) -> Path:
    """Write 16-bit PCM, matching the proven output format."""
    import soundfile as sf

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp.wav")
    sf.write(str(temporary), audio, sample_rate, subtype="PCM_16")
    temporary.replace(path)
    return path


def write_mp3(
    path: Path, wav_path: Path, bitrate: str = "192k", ffmpeg: str = "ffmpeg"
) -> Path:
    """Transcode the finished WAV to MP3 alongside it."""
    command = [
        ffmpeg, "-y", "-loglevel", "error",
        "-i", str(wav_path), "-codec:a", "libmp3lame", "-b:a", bitrate,
        str(path),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True)
    except FileNotFoundError as exc:
        raise AudioError(
            "FFmpeg was not found, so the MP3 could not be created.",
            suggestion="Install it with: brew install ffmpeg. The WAV was still written.",
            cause=exc,
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise AudioError(
            "FFmpeg could not create the MP3.",
            suggestion="The WAV export succeeded and can be used instead.",
            detail=exc.stderr.decode("utf-8", "replace")[:2000],
            cause=exc,
        ) from exc
    return path


def summarise_safety(plans: list[FitPlan]) -> dict[SpeedSafety, int]:
    counts: dict[SpeedSafety, int] = {}
    for plan in plans:
        counts[plan.safety] = counts.get(plan.safety, 0) + 1
    return counts
