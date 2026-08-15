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
    # A full QApplication, not QGuiApplication: the crop editor is a widget,
    # and a widget under a gui-only application aborts the process outright.
    # Only one kind can exist per process, so the whole file uses the bigger one.
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


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


# -- cropping ------------------------------------------------------------


@pytest.fixture
def two_tone_video(tmp_path: Path) -> Path:
    """Left half red, right half blue — so a crop reveals what it kept."""
    path = tmp_path / "twotone.mp4"
    with av.open(str(path), mode="w") as container:
        stream = container.add_stream("libx264", rate=10)
        stream.width, stream.height = 320, 180
        stream.pix_fmt = "yuv420p"
        picture = np.zeros((180, 320, 3), dtype=np.uint8)
        picture[:, :160] = (200, 30, 30)
        picture[:, 160:] = (30, 30, 200)
        for index in range(20):
            frame = av.VideoFrame.from_ndarray(picture, format="rgb24")
            frame.pts = index
            container.mux(stream.encode(frame))
        container.mux(stream.encode(None))
    return path


class TestCropMaths:
    from app.video.crop import CropSpec

    def test_dimensions_are_always_even(self):
        from app.video.crop import CropSpec

        # 9:16 of 1080 gives 607.5 — an encoder would refuse the odd number.
        _x, _y, w, h = CropSpec(9, 16).rect(1920, 1080)
        assert w % 2 == 0 and h % 2 == 0
        assert h == 1080 and w in (606, 608)

    def test_square_of_landscape_cuts_the_sides(self):
        from app.video.crop import CropSpec

        x, y, w, h = CropSpec(1, 1).rect(320, 180)
        assert (w, h) == (180, 180)
        assert y == 0
        assert x == 70          # centred by default

    def test_pan_slides_along_the_cut_axis_and_clamps(self):
        from app.video.crop import CropSpec

        assert CropSpec(1, 1, pan=0.0).rect(320, 180)[0] == 0
        assert CropSpec(1, 1, pan=1.0).rect(320, 180)[0] == 140
        assert CropSpec(1, 1, pan=9.9).rect(320, 180)[0] == 140   # clamped

    def test_a_wider_target_cuts_top_and_bottom_instead(self):
        from app.video.crop import CropSpec

        x, y, w, h = CropSpec(16, 9, pan=0.0).rect(180, 320)   # portrait source
        assert w == 180
        assert h < 320
        assert y == 0 and x == 0

    def test_same_shape_keeps_the_whole_frame(self):
        from app.video.crop import CropSpec

        assert CropSpec(16, 9).rect(1920, 1080) == (0, 0, 1920, 1080)

    def test_unknown_choice_maps_to_no_crop(self):
        from app.video.crop import crop_for

        assert crop_for("original") is None
        assert crop_for("nonsense") is None
        assert crop_for("9:16").label == "9:16"


class TestFreeCrop:
    def test_fractions_become_even_pixels(self):
        from app.video.crop import FreeCrop

        x, y, w, h = FreeCrop(0.25, 0.25, 0.5, 0.5).rect(1920, 1080)
        assert (x, y) == (480, 270)
        assert (w, h) == (960, 540)
        assert w % 2 == 0 and h % 2 == 0

    def test_out_of_range_values_are_pulled_back_into_the_frame(self):
        from app.video.crop import FreeCrop

        # A drag can momentarily produce nonsense; the rect must never.
        x, y, w, h = FreeCrop(-0.4, 1.7, 3.0, -1.0).rect(1920, 1080)
        assert 0 <= x <= 1920 - w
        assert 0 <= y <= 1080 - h
        assert w >= 16 and h >= 16

    def test_a_sliver_is_widened_to_something_encodable(self):
        from app.video.crop import FreeCrop

        _x, _y, w, h = FreeCrop(0.5, 0.5, 0.001, 0.001).rect(320, 180)
        assert w >= 16 and h >= 16

    def test_full_frame_stays_full_frame(self):
        from app.video.crop import FreeCrop

        assert FreeCrop(0.0, 0.0, 1.0, 1.0).rect(1920, 1080) == (0, 0, 1920, 1080)


def test_free_crop_keeps_the_drawn_region(qt_app, two_tone_video: Path, tmp_path: Path):
    """A rectangle drawn over the left half must come back red."""
    from app.video.crop import FreeCrop

    result = export_video(
        VideoExportRequest(
            video_path=two_tone_video, output_path=tmp_path / "drawn.mp4",
            crop=FreeCrop(0.0, 0.0, 0.45, 1.0),
        )
    )
    assert result.reencoded
    picture = frame_at(result.path, 0.5)
    assert picture.shape[1] < 320
    red = (picture[:, :, 0].astype(int) - picture[:, :, 2]).mean()
    assert red > 40, "the crop kept the wrong part of the frame"


class TestCropBoxInteraction:
    """Driving the editor's drag logic the way mouse events would."""

    def _editor(self, qt_app):
        """An editor whose widget and frame are both 640x360, so a widget pixel
        is exactly a frame pixel — the widget's own minimum height (230) would
        silently letterbox anything shorter and shift every coordinate."""
        from PySide6.QtGui import QImage

        from app.ui.widgets.cropbox import CropBox

        editor = CropBox()
        editor.resize(640, 360)
        editor.set_frame(QImage(640, 360, QImage.Format.Format_RGB888))
        area = editor._image_rect()
        assert (area.left(), area.top()) == (0, 0), "mapping must be 1:1 for these tests"
        return editor

    def test_dragging_the_middle_moves_the_rectangle(self, qt_app):
        from PySide6.QtCore import QPointF

        from app.video.crop import FreeCrop

        editor = self._editor(qt_app)
        editor.set_crop(FreeCrop(0.1, 0.1, 0.5, 0.5))
        committed = []
        editor.committed.connect(committed.append)

        editor.begin(QPointF(224, 126))         # centre of the rectangle
        editor.drag(QPointF(288, 162))          # +64px right, +36px down = +0.1 each
        editor.finish()

        crop = committed[-1]
        assert crop.left == pytest.approx(0.2, abs=0.01)
        assert crop.top == pytest.approx(0.2, abs=0.01)
        assert crop.width == pytest.approx(0.5, abs=0.01)   # size unchanged

    def test_pulling_an_edge_resizes_only_that_edge(self, qt_app):
        from PySide6.QtCore import QPointF

        from app.video.crop import FreeCrop

        editor = self._editor(qt_app)
        editor.set_crop(FreeCrop(0.25, 0.25, 0.5, 0.5))
        editor.begin(QPointF(480, 180))         # the right edge, mid-height
        editor.drag(QPointF(576, 180))          # pull it 96px further right
        editor.finish()

        crop = editor.crop()
        assert crop.left == pytest.approx(0.25, abs=0.01)
        assert crop.width == pytest.approx(0.65, abs=0.01)
        assert crop.top == pytest.approx(0.25, abs=0.01)

    def test_the_rectangle_cannot_be_dragged_out_of_the_frame(self, qt_app):
        from PySide6.QtCore import QPointF

        from app.video.crop import FreeCrop

        editor = self._editor(qt_app)
        editor.set_crop(FreeCrop(0.1, 0.1, 0.5, 0.5))
        editor.begin(QPointF(224, 126))
        editor.drag(QPointF(2000, 2000))        # a wild fling off the widget
        editor.finish()

        crop = editor.crop().normalised()
        assert crop.left + crop.width <= 1.0
        assert crop.top + crop.height <= 1.0

    def test_drawing_on_the_dimmed_area_starts_a_fresh_rectangle(self, qt_app):
        from PySide6.QtCore import QPointF

        from app.video.crop import FreeCrop

        editor = self._editor(qt_app)
        editor.set_crop(FreeCrop(0.6, 0.6, 0.3, 0.3))
        editor.begin(QPointF(64, 36))           # far outside, top-left area
        editor.drag(QPointF(320, 180))
        editor.finish()

        crop = editor.crop()
        assert crop.left == pytest.approx(0.1, abs=0.02)
        assert crop.top == pytest.approx(0.1, abs=0.02)
        assert crop.width == pytest.approx(0.4, abs=0.03)

    def test_edges_cannot_cross(self, qt_app):
        from PySide6.QtCore import QPointF

        from app.video.crop import FreeCrop

        editor = self._editor(qt_app)
        editor.set_crop(FreeCrop(0.25, 0.25, 0.5, 0.5))
        editor.begin(QPointF(480, 180))         # right edge
        editor.drag(QPointF(0, 180))            # dragged left past the left edge
        editor.finish()

        crop = editor.crop()
        assert crop.width >= crop.MIN_FRACTION - 1e-9


def test_crop_changes_the_output_dimensions(qt_app, two_tone_video: Path, tmp_path: Path):
    from app.video.crop import CropSpec

    result = export_video(
        VideoExportRequest(
            video_path=two_tone_video, output_path=tmp_path / "square.mp4",
            crop=CropSpec(1, 1),
        )
    )
    assert result.reencoded
    assert (result.width, result.height) == (180, 180)
    picture = frame_at(result.path, 0.5)
    assert picture.shape[:2] == (180, 180)


def test_pan_decides_which_part_survives(qt_app, two_tone_video: Path, tmp_path: Path):
    """Keep the left edge: the red half. Keep the right edge: the blue half."""
    from app.video.crop import CropSpec

    def dominant(pan: float, name: str) -> str:
        result = export_video(
            VideoExportRequest(
                video_path=two_tone_video, output_path=tmp_path / name,
                crop=CropSpec(1, 1, pan=pan),
            )
        )
        picture = frame_at(result.path, 0.5)
        red = (picture[:, :, 0].astype(int) - picture[:, :, 2]).mean()
        return "red" if red > 40 else "blue" if red < -40 else "mixed"

    assert dominant(0.0, "left.mp4") == "red"
    assert dominant(1.0, "right.mp4") == "blue"


def test_crop_and_burned_subtitles_compose(qt_app, two_tone_video: Path, tmp_path: Path):
    """The caption must be laid out for the cropped picture, not the original."""
    from app.video.crop import CropSpec

    result = export_video(
        VideoExportRequest(
            video_path=two_tone_video, output_path=tmp_path / "both.mp4",
            segments=[Segment(0, 2000, "Cropped and captioned.")],
            burn_subtitles=True, crop=CropSpec(1, 1),
            style=SubtitleStyle(colour="#FFD54A"),
        )
    )
    assert (result.width, result.height) == (180, 180)
    assert result.burned_captions == 1

    picture = frame_at(result.path, 0.5)
    assert picture.shape[:2] == (180, 180)
    yellow = ((picture[:, :, 0] > 170) & (picture[:, :, 1] > 140)
              & (picture[:, :, 2] < 130)).sum()
    assert yellow > 20, "the caption did not survive the crop"


def test_crop_alone_does_not_burn_captions(qt_app, two_tone_video: Path, tmp_path: Path):
    """Re-encoding for a crop must not drag subtitles in uninvited."""
    from app.video.crop import CropSpec

    result = export_video(
        VideoExportRequest(
            video_path=two_tone_video, output_path=tmp_path / "clean.mp4",
            segments=[Segment(0, 2000, "Should not appear.")],
            crop=CropSpec(1, 1), style=SubtitleStyle(colour="#FFD54A"),
        )
    )
    assert result.burned_captions == 0
    picture = frame_at(result.path, 0.5)
    yellow = ((picture[:, :, 0] > 170) & (picture[:, :, 1] > 140)
              & (picture[:, :, 2] < 130)).sum()
    assert yellow == 0, "captions were burned in without being asked for"


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
