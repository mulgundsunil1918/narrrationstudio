"""Media decoding, encoding and time-stretching, with no FFmpeg to install.

Every audio job in this app used to shell out to ``ffmpeg`` or ``ffprobe``, so a
new user's first experience was being told to open Terminal and run Homebrew.
That is a fair amount to ask of someone who wanted to put a voice on a video,
and it is the single most common reason the app does not work on a fresh Mac.

PyAV is FFmpeg — the same libraries, compiled into the Python wheel — and it is
already here because the transcription engine depends on it. So the work happens
in-process against those libraries, and nothing has to be installed.

The command-line path is kept as a fallback for anyone whose environment lacks
PyAV. It is genuinely a fallback: :func:`is_available` reports which one is in
use, and the pre-flight check only fails when *neither* is present.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from app.core.errors import AudioError

logger = logging.getLogger(__name__)


def _av():
    """Import PyAV, or return None if this environment does not have it."""
    try:
        import av

        return av
    except ImportError:
        return None


def have_pyav() -> bool:
    return _av() is not None


def have_ffmpeg_binary(name: str = "ffmpeg") -> bool:
    return shutil.which(name) is not None


def is_available() -> tuple[bool, str]:
    """Whether media work can be done at all, and by what."""
    module = _av()
    if module is not None:
        return True, f"built in (FFmpeg {module.ffmpeg_version_info})"
    if have_ffmpeg_binary():
        return True, "installed FFmpeg"
    return False, ""


# -- probing -------------------------------------------------------------


@dataclass(frozen=True)
class MediaInfo:
    duration_ms: int = 0
    has_audio: bool = False
    has_video: bool = False
    readable: bool = True


def probe(path: Path) -> MediaInfo:
    """Read a file's duration and stream types.

    An unreadable file comes back as ``readable=False`` rather than raising:
    callers use this to decide what to offer, and the real error belongs to the
    operation that actually tries to use the file.
    """
    module = _av()
    if module is None:
        return _probe_with_ffprobe(path)

    try:
        with module.open(str(path)) as container:
            duration_ms = (
                int(container.duration / module.time_base * 1000)
                if container.duration is not None
                else 0
            )
            audio = list(container.streams.audio)
            # A container duration can be absent on a stream; fall back to the
            # audio stream's own, which is what matters for transcription.
            if not duration_ms and audio:
                stream = audio[0]
                if stream.duration is not None and stream.time_base:
                    duration_ms = int(stream.duration * stream.time_base * 1000)
            return MediaInfo(
                duration_ms=max(0, duration_ms),
                has_audio=bool(audio),
                has_video=bool(list(container.streams.video)),
            )
    except Exception as exc:
        logger.debug("Could not probe %s: %s", path.name, exc)
        return MediaInfo(readable=False)


def _probe_with_ffprobe(path: Path, ffprobe: str = "ffprobe") -> MediaInfo:
    try:
        duration = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, check=True, timeout=60,
        )
        streams = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "stream=codec_type",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, check=True, timeout=60,
        )
    except Exception:
        return MediaInfo(readable=False)

    try:
        duration_ms = int(round(float(duration.stdout.decode().strip()) * 1000))
    except ValueError:
        duration_ms = 0
    kinds = streams.stdout.decode()
    return MediaInfo(
        duration_ms=max(0, duration_ms),
        has_audio="audio" in kinds,
        has_video="video" in kinds,
    )


# -- decoding ------------------------------------------------------------


def decode_to_wav(source: Path, destination: Path, rate: int = 16_000) -> Path:
    """Write ``source``'s audio as mono 16-bit WAV at ``rate``."""
    module = _av()
    if module is None:
        return _decode_with_ffmpeg(source, destination, rate)

    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with module.open(str(source)) as container:
            if not container.streams.audio:
                raise AudioError(
                    f"“{source.name}” has no sound in it.",
                    reason="The file contains no audio track at all.",
                    suggestion=(
                        "If this is a silent screen recording, write the script "
                        "instead and bring it here as text."
                    ),
                )
            stream = container.streams.audio[0]
            resampler = module.AudioResampler(format="s16", layout="mono", rate=rate)

            with module.open(str(destination), mode="w") as output:
                out_stream = output.add_stream("pcm_s16le", rate=rate)
                out_stream.layout = "mono"
                for frame in container.decode(stream):
                    for resampled in resampler.resample(frame):
                        # A resampled frame carries the input's timestamps, which
                        # the WAV muxer then rejects as non-monotonic.
                        resampled.pts = None
                        output.mux(out_stream.encode(resampled))
                for resampled in resampler.resample(None):
                    resampled.pts = None
                    output.mux(out_stream.encode(resampled))
                output.mux(out_stream.encode(None))
    except AudioError:
        raise
    except Exception as exc:
        raise AudioError(
            "The audio could not be read out of that file.",
            reason="It could not be decoded, so the format may be unsupported.",
            suggestion="Try exporting the video again, or use a .mp4 or .mov file.",
            cause=exc,
        ) from exc

    if not destination.exists() or destination.stat().st_size <= 44:
        raise AudioError(
            "That file does not seem to contain any audio.",
            reason="Reading it produced an empty audio track.",
            suggestion=(
                "If this is a silent screen recording, there is nothing to "
                "transcribe — write the script instead and import it as text."
            ),
        )
    return destination


def _decode_with_ffmpeg(
    source: Path, destination: Path, rate: int, ffmpeg: str = "ffmpeg"
) -> Path:
    if not shutil.which(ffmpeg):
        raise AudioError(
            "The audio could not be read out of your video.",
            reason="This installation has neither the built-in decoder nor FFmpeg.",
            suggestion="Reinstall the app, which includes everything it needs.",
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg, "-y", "-loglevel", "error", "-i", str(source),
        "-vn", "-ac", "1", "-ar", str(rate), "-c:a", "pcm_s16le", str(destination),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, timeout=1800)
    except subprocess.CalledProcessError as exc:
        raise AudioError(
            "The audio could not be read out of that file.",
            reason="It could not be decoded, so the format may be unsupported.",
            suggestion="Try exporting the video again, or use a .mp4 or .mov file.",
            detail=exc.stderr.decode("utf-8", "replace")[:2000],
            cause=exc,
        ) from exc
    return destination


# -- time stretching -----------------------------------------------------


def time_stretch(audio: np.ndarray, factor: float, sample_rate: int) -> np.ndarray:
    """Speed audio up or down without changing its pitch.

    ``factor`` above 1.0 makes it shorter. Returns the input unchanged when the
    change would be inaudible.
    """
    if abs(factor - 1.0) < 1e-3 or audio.size == 0:
        return audio

    module = _av()
    if module is None:
        raise AudioError(
            "Speech could not be fitted to its window.",
            reason="This installation has no audio filter available.",
            suggestion="Reinstall the app, which includes everything it needs.",
        )

    from fractions import Fraction

    from app.audio.timing import atempo_stages

    samples = np.ascontiguousarray(audio, dtype=np.float32)
    try:
        graph = module.filter.Graph()
        source = graph.add_abuffer(
            format="fltp", sample_rate=sample_rate, layout="mono",
            time_base=Fraction(1, sample_rate),
        )
        previous = source
        for stage in atempo_stages(factor):
            node = graph.add("atempo", f"{stage:.6f}")
            previous.link_to(node)
            previous = node
        sink = graph.add("abuffersink")
        previous.link_to(sink)
        graph.configure()

        frame = module.AudioFrame.from_ndarray(
            samples.reshape(1, -1), format="fltp", layout="mono"
        )
        frame.sample_rate = sample_rate
        frame.pts = 0
        graph.push(frame)
        graph.push(None)

        chunks: list[np.ndarray] = []
        while True:
            try:
                out = graph.pull()
            except (StopIteration, module.error.EOFError, module.error.BlockingIOError):
                break
            chunks.append(out.to_ndarray().reshape(-1).astype(np.float32))
    except AudioError:
        raise
    except Exception as exc:
        raise AudioError(
            "Speech could not be fitted to its window.",
            reason="The speed adjustment failed.",
            suggestion="Try generating again, or turn off timing adjustment in Settings.",
            cause=exc,
        ) from exc

    if not chunks:
        return audio
    return np.concatenate(chunks)


# -- encoding ------------------------------------------------------------


def encode_mp3(destination: Path, wav_path: Path, bitrate: str = "192k") -> Path:
    """Transcode a WAV to MP3."""
    module = _av()
    if module is None:
        return _encode_mp3_with_ffmpeg(destination, wav_path, bitrate)

    rate = int(bitrate.rstrip("k")) * 1000 if bitrate.endswith("k") else int(bitrate)
    try:
        with module.open(str(wav_path)) as source:
            in_stream = source.streams.audio[0]
            with module.open(str(destination), mode="w") as output:
                out_stream = output.add_stream("libmp3lame", rate=in_stream.rate)
                out_stream.bit_rate = rate
                resampler = module.AudioResampler(
                    format=out_stream.format.name,
                    layout=out_stream.layout.name,
                    rate=out_stream.rate,
                )
                for frame in source.decode(in_stream):
                    for resampled in resampler.resample(frame):
                        resampled.pts = None
                        output.mux(out_stream.encode(resampled))
                for resampled in resampler.resample(None):
                    resampled.pts = None
                    output.mux(out_stream.encode(resampled))
                output.mux(out_stream.encode(None))
    except Exception as exc:
        raise AudioError(
            "The MP3 could not be created.",
            reason="The audio could not be encoded.",
            suggestion="Use the WAV instead — it was written successfully.",
            cause=exc,
        ) from exc
    return destination


def _encode_mp3_with_ffmpeg(
    destination: Path, wav_path: Path, bitrate: str, ffmpeg: str = "ffmpeg"
) -> Path:
    if not shutil.which(ffmpeg):
        raise AudioError(
            "The MP3 could not be created.",
            reason="This installation has no MP3 encoder available.",
            suggestion="Use the WAV instead — it was written successfully.",
        )
    try:
        subprocess.run(
            [ffmpeg, "-y", "-loglevel", "error", "-i", str(wav_path),
             "-codec:a", "libmp3lame", "-b:a", bitrate, str(destination)],
            check=True, capture_output=True, timeout=1800,
        )
    except subprocess.CalledProcessError as exc:
        raise AudioError(
            "The MP3 could not be created.",
            reason="The encoder rejected the audio.",
            suggestion="Use the WAV instead — it was written successfully.",
            detail=exc.stderr.decode("utf-8", "replace")[:2000],
            cause=exc,
        ) from exc
    return destination
