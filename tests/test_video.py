"""Putting the narration, and the subtitles, back onto the video.

Two things are worth guarding here. Replacing the audio must not touch the
picture — re-encoding a screen recording to swap its soundtrack throws away
quality for nothing. And burned-in subtitles must actually reach the pixels: a
file that writes successfully but comes out blank is the failure a user only
discovers after uploading it somewhere.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from app.core.errors import StudioError
from app.core.models import Segment
from app.video.export import VideoExportRequest, container_format, export_video
from app.video.style import PRESETS, SubtitleStyle, preset

av = pytest.importorskip("av")

SEGMENTS = [
    Segment(0, 2000, "Check the blood glucose first."),
    Segment(2000, 4000, "Then set the infusion rate."),
]


@pytest.fixture(scope="module")
def qt_app():
    from PySide6.QtGui import QGuiApplication

    return QGuiApplication.instance() or QGuiApplication([])


@pytest.fixture
def video(tmp_path: Path) -> Path:
    """A short solid-colour video with a silent audio track."""
    path = tmp_path / "source.mp4"
    with av.open(str(path), mode="w") as container:
        stream = container.add_stream("libx264", rate=10)
        stream.width, stream.height = 320, 180
        stream.pix_fmt = "yuv420p"
        picture = np.zeros((180, 320, 3), dtype=np.uint8)
        picture[:, :] = (10, 20, 60)          # solid navy: one colour, easy to test
        for index in range(40):               # four seconds
            frame = av.VideoFrame.from_ndarray(picture, format="rgb24")
            frame.pts = index
            container.mux(stream.encode(frame))
        container.mux(stream.encode(None))
    return path


@pytest.fixture
def narration(tmp_path: Path) -> Path:
    import wave

    path = tmp_path / "narration.wav"
    rate = 24_000
    tone = (np.sin(2 * np.pi * 330 * np.arange(rate * 4) / rate) * 0.4).astype(np.float32)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes((tone * 32767).astype("<i2").tobytes())
    return path


def frame_at(path: Path, seconds: float) -> np.ndarray:
    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        for frame in container.decode(stream):
            if frame.pts is not None and float(frame.pts * stream.time_base) >= seconds:
                return frame.to_ndarray(format="rgb24")
    raise AssertionError(f"no frame at {seconds}s")


# -- the fast path -------------------------------------------------------


def test_replacing_audio_leaves_the_picture_untouched(
    qt_app, video: Path, narration: Path, tmp_path: Path
):
    """A soundtrack swap must not cost a generation of video quality."""
    before = frame_at(video, 1.0)
    result = export_video(
        VideoExportRequest(
            video_path=video, output_path=tmp_path / "out.mp4", audio_path=narration
        )
    )

    assert not result.reencoded
    assert np.array_equal(frame_at(result.path, 1.0), before), (
        "the picture changed, so it was re-encoded when it did not need to be"
    )
    with av.open(str(result.path)) as container:
        assert container.streams.audio, "the narration was not attached"


def test_the_output_has_both_streams(qt_app, video: Path, narration: Path, tmp_path: Path):
    result = export_video(
        VideoExportRequest(
            video_path=video, output_path=tmp_path / "out.mp4", audio_path=narration
        )
    )
    with av.open(str(result.path)) as container:
        kinds = sorted(s.type for s in container.streams)
    assert kinds == ["audio", "video"]


def test_a_sidecar_srt_is_written_beside_the_video(
    qt_app, video: Path, narration: Path, tmp_path: Path
):
    result = export_video(
        VideoExportRequest(
            video_path=video, output_path=tmp_path / "out.mp4", audio_path=narration,
            segments=SEGMENTS, sidecar_subtitles=True,
        )
    )
    assert result.subtitle_path is not None
    assert result.subtitle_path.name == "out.srt"
    body = result.subtitle_path.read_text()
    assert "Check the blood glucose first." in body
    assert "00:00:00,000 --> 00:00:02,000" in body


# -- burning in ----------------------------------------------------------


def test_burned_subtitles_reach_the_pixels(qt_app, video: Path, tmp_path: Path):
    """The test that matters: a file that writes but shows nothing is the bug."""
    result = export_video(
        VideoExportRequest(
            video_path=video, output_path=tmp_path / "burned.mp4",
            segments=SEGMENTS, burn_subtitles=True,
            style=SubtitleStyle(colour="#FFD54A"),
        )
    )
    assert result.reencoded
    assert result.burned_captions == 2

    picture = frame_at(result.path, 1.0)
    yellow = ((picture[:, :, 0] > 170) & (picture[:, :, 1] > 140)
              & (picture[:, :, 2] < 130)).sum()
    assert yellow > 30, "no caption pixels were found in the frame"


def test_captions_appear_only_while_they_are_on_screen(
    qt_app, video: Path, tmp_path: Path
):
    """A caption bleeding outside its own window is worse than none at all."""
    result = export_video(
        VideoExportRequest(
            video_path=video, output_path=tmp_path / "timed.mp4",
            segments=[Segment(2000, 4000, "Only in the second half.")],
            burn_subtitles=True, style=SubtitleStyle(colour="#FFD54A"),
        )
    )

    def caption_pixels(seconds: float) -> int:
        picture = frame_at(result.path, seconds)
        return ((picture[:, :, 0] > 170) & (picture[:, :, 1] > 140)
                & (picture[:, :, 2] < 130)).sum()

    assert caption_pixels(0.5) == 0, "the caption showed before its start time"
    assert caption_pixels(2.5) > 30, "the caption was missing during its window"


def test_position_moves_the_caption(qt_app, video: Path, tmp_path: Path):
    def rows_used(position: str) -> tuple[int, int]:
        result = export_video(
            VideoExportRequest(
                video_path=video, output_path=tmp_path / f"{position}.mp4",
                segments=[Segment(0, 4000, "Where am I?")], burn_subtitles=True,
                style=SubtitleStyle(colour="#FFD54A", position=position),
            )
        )
        picture = frame_at(result.path, 1.0)
        mask = ((picture[:, :, 0] > 170) & (picture[:, :, 1] > 140)
                & (picture[:, :, 2] < 130))
        rows = np.where(mask.any(axis=1))[0]
        assert len(rows), f"nothing was drawn for position={position}"
        return int(rows.min()), int(rows.max())

    top = rows_used("top")
    bottom = rows_used("bottom")
    assert top[1] < bottom[0], "top and bottom placed the caption in the same band"


# -- failures ------------------------------------------------------------


def test_a_missing_video_is_reported(qt_app, tmp_path: Path):
    with pytest.raises(StudioError) as caught:
        export_video(
            VideoExportRequest(
                video_path=tmp_path / "gone.mp4", output_path=tmp_path / "out.mp4"
            )
        )
    assert "could not be found" in caught.value.message


def test_missing_narration_is_reported(qt_app, video: Path, tmp_path: Path):
    with pytest.raises(StudioError) as caught:
        export_video(
            VideoExportRequest(
                video_path=video, output_path=tmp_path / "out.mp4",
                audio_path=tmp_path / "never-generated.wav",
            )
        )
    assert caught.value.suggestion


def test_an_audio_only_file_is_refused(qt_app, narration: Path, tmp_path: Path):
    with pytest.raises(StudioError) as caught:
        export_video(
            VideoExportRequest(
                video_path=narration, output_path=tmp_path / "out.mp4"
            )
        )
    assert "no picture" in caught.value.message


def test_a_failed_export_leaves_no_half_written_file(qt_app, tmp_path: Path):
    """A .part file left behind would look like a finished export next time."""
    target = tmp_path / "out.mp4"
    with pytest.raises(StudioError):
        export_video(
            VideoExportRequest(
                video_path=tmp_path / "missing.mp4", output_path=target
            )
        )
    assert not target.exists()
    assert not list(tmp_path.glob("*.part"))


# -- style ---------------------------------------------------------------


def test_sizes_are_relative_so_a_style_survives_a_resolution_change():
    style = SubtitleStyle(size_percent=5.0)
    assert style.scaled_to(1080).font_size == 54
    assert style.scaled_to(2160).font_size == 108   # 4K: twice the pixels, same look


def test_every_preset_is_usable():
    for name, description, style in PRESETS:
        assert description
        assert preset(name) == style
        assert style.position in ("top", "middle", "bottom")
        assert style.alignment in ("left", "center", "right")


def test_an_unknown_preset_falls_back_rather_than_failing():
    assert preset("no such preset") == SubtitleStyle()


def test_container_format_follows_the_real_extension(tmp_path: Path):
    # The output is written to a ".part" file, so the format cannot be guessed.
    assert container_format(Path("a.mp4")) == "mp4"
    assert container_format(Path("a.mov")) == "mov"
    assert container_format(Path("a.mkv")) == "matroska"
    assert container_format(Path("a.weird")) == "mp4"


# -- caption drawing -----------------------------------------------------


def test_a_blank_caption_draws_nothing(qt_app):
    from app.video.captions import render_caption

    assert render_caption("   ", SubtitleStyle(), 1280, 720) is None


def test_a_caption_stays_inside_the_frame(qt_app):
    from app.video.captions import render_caption

    long_text = "This is a very long caption " * 6
    layer = render_caption(long_text, SubtitleStyle(), 640, 360)
    assert layer is not None
    assert layer.x >= 0 and layer.y >= 0
    assert layer.x + layer.width <= 640
    assert layer.y + layer.height <= 360
