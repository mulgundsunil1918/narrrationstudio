"""Put the narration — and optionally the subtitles — back onto the video.

Three things can be asked for, and they cost very different amounts:

* Replacing the audio copies the video stream through untouched. Nothing is
  re-encoded, so the picture is bit-for-bit the original and a long video
  finishes in seconds.
* Soft subtitles add a track the viewer can switch off. Still no re-encoding.
* Burned-in subtitles have to be painted into the picture, so the video is
  decoded and encoded again. It is the slow one, and it is the only one that
  loses a generation of quality — which is why it is not the default.

All of it runs against the FFmpeg libraries that ship with the app.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

import numpy as np

from app.core.errors import AudioError, StudioError
from app.core.models import Segment
from app.video.captions import CaptionLayer, blend, render_caption
from app.video.crop import CropSpec, FreeCrop
from app.video.style import SubtitleStyle

logger = logging.getLogger(__name__)

#: Quality for the re-encode. 18 is visually lossless for screen recordings;
#: lower numbers are bigger files for differences nobody sees.
DEFAULT_CRF = 18

ProgressCallback = Callable[[float | None, str], None]


@dataclass
class VideoExportRequest:
    video_path: Path
    output_path: Path
    #: The narration WAV. None keeps whatever audio the video already had.
    audio_path: Path | None = None
    segments: Sequence[Segment] = field(default_factory=tuple)
    #: Write ``<video>.srt`` beside the output. Free, switchable in the player,
    #: and the only route to a separate subtitle track here: the FFmpeg build
    #: that ships with the app cannot open a subtitle *encoder*, so a track
    #: inside the file is not something this can honestly offer.
    sidecar_subtitles: bool = False
    #: Painted into the picture. Requires re-encoding the video.
    burn_subtitles: bool = False
    style: SubtitleStyle = field(default_factory=SubtitleStyle)
    #: Cut the picture down: a preset shape or a hand-drawn rectangle. Either
    #: way it requires re-encoding — a crop that kept the original bitstream
    #: would be no crop at all.
    crop: "CropSpec | FreeCrop | None" = None
    crf: int = DEFAULT_CRF

    @property
    def needs_reencode(self) -> bool:
        return self.burn_subtitles or self.crop is not None


@dataclass
class VideoExportResult:
    path: Path
    width: int = 0
    height: int = 0
    duration_ms: int = 0
    reencoded: bool = False
    burned_captions: int = 0
    #: The .srt written beside the video, when one was asked for.
    subtitle_path: Path | None = None
    warnings: list[str] = field(default_factory=list)


#: The output is written to a ".part" file first, which leaves the container
#: format unguessable from the name — so it is stated outright.
CONTAINERS = {
    ".mp4": "mp4", ".m4v": "mp4", ".mov": "mov",
    ".mkv": "matroska", ".webm": "webm",
}


def container_format(path: Path) -> str:
    return CONTAINERS.get(path.suffix.lower(), "mp4")


def _av():
    try:
        import av

        return av
    except ImportError as exc:
        raise StudioError(
            "This installation cannot write video files.",
            reason="The video components that ship with the app are missing.",
            suggestion="Reinstall Narration Studio.",
            cause=exc,
        ) from exc


def export_video(
    request: VideoExportRequest,
    on_progress: ProgressCallback | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> VideoExportResult:
    """Write the finished video. Raises StudioError with something to act on."""
    av = _av()
    source = Path(request.video_path)
    if not source.exists():
        raise StudioError(
            f"“{source.name}” could not be found.",
            suggestion="Import the video again, then export.",
        )
    if request.audio_path is not None and not Path(request.audio_path).exists():
        raise StudioError(
            "The narration audio could not be found.",
            reason="It may have been moved or deleted since it was generated.",
            suggestion="Generate the narration again, then export.",
        )

    request.output_path.parent.mkdir(parents=True, exist_ok=True)
    # Write beside the target and move it into place at the end, so a failure
    # or a cancellation never leaves a half-written file where a finished one
    # is expected.
    working = request.output_path.with_name(request.output_path.name + ".part")

    try:
        if request.needs_reencode:
            result = _export_reencoded(av, request, working, on_progress, should_cancel)
        else:
            result = _export_stream_copy(av, request, working, on_progress, should_cancel)
    except StudioError:
        working.unlink(missing_ok=True)
        raise
    except Exception as exc:
        working.unlink(missing_ok=True)
        raise StudioError(
            "The video could not be written.",
            reason=str(exc),
            suggestion="Try again, or export the audio on its own and use your editor.",
            cause=exc,
        ) from exc

    if result is None:      # cancelled
        working.unlink(missing_ok=True)
        raise Cancelled()

    working.replace(request.output_path)
    result.path = request.output_path

    if request.sidecar_subtitles and request.segments:
        written = sidecar_subtitles(request.output_path, request.segments)
        if written is not None:
            result.subtitle_path = written
    logger.info(
        "Exported %s (%dx%d, re-encoded=%s, %d captions burned)",
        request.output_path.name, result.width, result.height,
        result.reencoded, result.burned_captions,
    )
    return result


class Cancelled(Exception):
    """The user stopped the export."""


# -- the fast path -------------------------------------------------------


def _export_stream_copy(
    av, request: VideoExportRequest, working: Path, on_progress, should_cancel
) -> VideoExportResult | None:
    """Swap the audio, and optionally add a subtitle track. No re-encoding."""
    if on_progress:
        on_progress(None, "Copying the picture across…")

    result = VideoExportResult(path=working)
    with av.open(str(request.video_path)) as source:
        if not source.streams.video:
            raise StudioError(
                f"“{Path(request.video_path).name}” has no picture in it.",
                reason="The file contains no video track.",
                suggestion="Choose the video file you want the narration on.",
            )
        video_in = source.streams.video[0]
        result.width = video_in.codec_context.width
        result.height = video_in.codec_context.height
        total = float(source.duration or 0) / av.time_base or 0.0

        with av.open(str(working), mode="w", format=container_format(request.output_path)) as output:
            video_out = output.add_stream_from_template(video_in)
            audio_out, resampler = _prepare_audio(av, output, request)

            for index, packet in enumerate(source.demux(video_in)):
                if should_cancel and should_cancel() and index % 50 == 0:
                    return None
                if packet.dts is None:
                    continue
                packet.stream = video_out
                output.mux(packet)
                if on_progress and total and index % 30 == 0:
                    elapsed = float(packet.pts or 0) * float(video_in.time_base)
                    on_progress(min(0.95, elapsed / total), "Copying the picture across…")

            if audio_out is not None:
                _write_audio(av, request, output, audio_out, resampler, on_progress)

    result.duration_ms = int(total * 1000)
    if on_progress:
        on_progress(1.0, "Finishing…")
    return result


def _prepare_audio(av, output, request: VideoExportRequest):
    if request.audio_path is None:
        return None, None
    with av.open(str(request.audio_path)) as probe:
        rate = probe.streams.audio[0].rate
    stream = output.add_stream("aac", rate=rate)
    resampler = av.AudioResampler(
        format=stream.format.name, layout=stream.layout.name, rate=stream.rate
    )
    return stream, resampler


def _write_audio(av, request, output, audio_out, resampler, on_progress) -> None:
    if on_progress:
        on_progress(None, "Adding the narration…")
    with av.open(str(request.audio_path)) as audio:
        stream = audio.streams.audio[0]
        for frame in audio.decode(stream):
            for resampled in resampler.resample(frame):
                resampled.pts = None
                output.mux(audio_out.encode(resampled))
    output.mux(audio_out.encode(None))


# -- the slow path -------------------------------------------------------


def _export_reencoded(
    av, request: VideoExportRequest, working: Path, on_progress, should_cancel
) -> VideoExportResult | None:
    """Paint the captions into the picture. Decodes and re-encodes the video."""
    result = VideoExportResult(path=working, reencoded=True)

    with av.open(str(request.video_path)) as source:
        if not source.streams.video:
            raise StudioError(
                f"“{Path(request.video_path).name}” has no picture in it.",
                reason="The file contains no video track.",
                suggestion="Choose the video file you want the narration on.",
            )
        video_in = source.streams.video[0]
        width = video_in.codec_context.width
        height = video_in.codec_context.height
        total = float(source.duration or 0) / av.time_base or 0.0

        # The crop is resolved once against the real dimensions; everything
        # downstream — output size, caption layout — works in the cropped frame.
        crop_rect = request.crop.rect(width, height) if request.crop else None
        if crop_rect:
            out_w, out_h = crop_rect[2], crop_rect[3]
        else:
            out_w, out_h = width, height
        result.width, result.height = out_w, out_h

        layers: list[tuple[int, int, CaptionLayer]] = []
        if request.burn_subtitles:
            if on_progress:
                on_progress(None, "Preparing the subtitles…")
            # Captions are laid out for the *cropped* picture, so their size and
            # margins are honest percentages of what the viewer will see.
            layers = _prepare_layers(request, out_w, out_h)
        result.burned_captions = len(layers)

        doing = "Adding subtitles to" if layers else "Reshaping"

        with av.open(str(working), mode="w", format=container_format(request.output_path)) as output:
            rate = video_in.average_rate or 30
            video_out = output.add_stream("libx264", rate=rate)
            video_out.width = out_w
            video_out.height = out_h
            video_out.pix_fmt = "yuv420p"
            # veryfast, not medium: at CRF 18 the difference is file size, not
            # anything visible, and a phone-resolution re-encode at medium is
            # minutes of "0%" that read as a hang.
            video_out.options = {"crf": str(request.crf), "preset": "veryfast"}
            # Phone recordings are variable-frame-rate: their "rate" is a
            # fraction like 140490000/4917011, and an encoder ticking at that
            # rate makes neighbouring source timestamps collide when rescaled —
            # frames 0ms and 8ms apart both land on tick 0, the muxer rejects
            # the duplicate ("Invalid argument", errno 22), and the export
            # dies. Both clocks must be the source's own: the codec context is
            # the one the encoder actually stamps packets with, and setting
            # only the stream's was not enough.
            video_out.codec_context.time_base = video_in.time_base
            video_out.time_base = video_in.time_base

            audio_out, resampler = _prepare_audio(av, output, request)

            frames = 0
            for frame in source.decode(video_in):
                if should_cancel and should_cancel() and frames % 20 == 0:
                    return None
                seconds = float(frame.pts * video_in.time_base) if frame.pts else 0.0
                picture = frame.to_ndarray(format="rgb24")

                if crop_rect:
                    x, y, crop_w, crop_h = crop_rect
                    picture = np.ascontiguousarray(
                        picture[y : y + crop_h, x : x + crop_w]
                    )

                for start, end, layer in layers:
                    if start <= seconds * 1000 < end:
                        blend(picture, layer)
                        break

                out_frame = av.VideoFrame.from_ndarray(picture, format="rgb24")
                out_frame.pts = frame.pts
                out_frame.time_base = frame.time_base
                output.mux(video_out.encode(out_frame))

                frames += 1
                if on_progress and total and frames % 15 == 0:
                    on_progress(
                        min(0.95, seconds / total),
                        f"{doing} the picture… frame {frames}",
                    )
            output.mux(video_out.encode(None))

            if audio_out is not None:
                _write_audio(av, request, output, audio_out, resampler, on_progress)

    result.duration_ms = int(total * 1000)
    if on_progress:
        on_progress(1.0, "Finishing…")
    return result


def _prepare_layers(
    request: VideoExportRequest, width: int, height: int
) -> list[tuple[int, int, CaptionLayer]]:
    """Draw every caption once, up front, rather than once per frame."""
    layers: list[tuple[int, int, CaptionLayer]] = []
    for segment in request.segments:
        layer = render_caption(segment.text, request.style, width, height)
        if layer is not None:
            layers.append((segment.start_ms, segment.end_ms, layer))
    return layers


def sidecar_subtitles(video_path: Path, segments: Sequence[Segment]) -> Path | None:
    """Write ``video.srt`` beside the video, which players pick up on their own."""
    if not segments:
        return None
    from app.srt.writer import write_srt

    target = video_path.with_suffix(".srt")
    try:
        write_srt(target, list(segments))
    except OSError as exc:
        raise AudioError(
            "The subtitle file could not be saved next to the video.",
            reason=str(exc),
            suggestion="The video itself was written; try saving the .srt elsewhere.",
            cause=exc,
        ) from exc
    return target
