"""The Video card's rules: when export is possible, and what audio it carries.

The trap being guarded: generated narration lives in memory, but the export
takes a file path. A WAV exists on disk only if the user pressed Export Audio
first — and gating on the memory while sending the path once meant a
generated-but-unsaved narration was silently left off the video. Silence where
the user's voice should be is the one outcome worse than an error.
"""

from __future__ import annotations

import wave
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from app.core.models import Segment


@pytest.fixture(scope="module")
def qt_app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def state(qt_app):
    from app.ui.state import AppState

    state = AppState()
    state.load_segments([Segment(0, 2000, "Check the glucose.")], None, name="demo")
    return state


@pytest.fixture
def panel(qt_app, state, tmp_path: Path):
    from app.ui.screens.video_panel import VideoPanel

    video = tmp_path / "clip.mp4"
    video.write_bytes(b"not read by these tests")
    state.media_path = video
    return VideoPanel(state)


def _give_narration(state, seconds: float = 2.0) -> None:
    rate = 24_000
    state.generated_audio = np.zeros(int(rate * seconds), dtype=np.float32)
    state.outcome = SimpleNamespace(sample_rate=rate)


# -- enablement ----------------------------------------------------------


def test_without_narration_a_plain_export_is_blocked_with_directions(panel):
    assert not panel._export.isEnabled()
    message = panel._status.text()
    assert "Generate the narration first" in message
    assert "reopened project" in message, (
        "the message must explain WHY the narration is missing"
    )


def test_subtitles_alone_are_a_legitimate_export(panel):
    """Burning subtitles onto a video that keeps its own sound must work."""
    panel._mode.select("burn")
    assert panel._export.isEnabled()
    assert "keeps its own" in panel._status.text()


def test_a_crop_alone_is_a_legitimate_export(panel):
    panel._crop_choice.select("9:16")
    assert panel._export.isEnabled()


def test_deselecting_the_only_reason_disables_export_again(panel):
    panel._mode.select("burn")
    assert panel._export.isEnabled()
    panel._mode.select("none")
    assert not panel._export.isEnabled()


def test_with_narration_export_is_ready_and_quiet(panel, state):
    _give_narration(state)
    panel._refresh()
    assert panel._export.isEnabled()
    assert panel._status.text() == ""


# -- which audio rides along ---------------------------------------------


def test_unsaved_narration_is_written_to_a_file_for_the_export(panel, state):
    """Generate, skip Export Audio, export the video: the voice must come too."""
    _give_narration(state, seconds=1.5)
    state.generated_path = None

    audio_path = panel._narration_file()
    assert audio_path is not None and audio_path.exists()
    with wave.open(str(audio_path), "rb") as handle:
        assert handle.getframerate() == 24_000
        assert handle.getnframes() == 24_000 + 12_000


def test_an_exported_wav_is_used_as_is(panel, state, tmp_path: Path):
    _give_narration(state)
    saved = tmp_path / "narration.wav"
    saved.write_bytes(b"RIFF")     # existence is what is checked here
    state.generated_path = saved
    assert panel._narration_file() == saved


def test_a_deleted_wav_falls_back_to_memory(panel, state, tmp_path: Path):
    """The saved file may be gone; the session's audio is still real."""
    _give_narration(state)
    state.generated_path = tmp_path / "moved-or-deleted.wav"

    audio_path = panel._narration_file()
    assert audio_path is not None and audio_path.exists()
    assert audio_path != state.generated_path


def test_no_narration_means_no_audio_swap(panel, state):
    state.generated_audio = None
    assert panel._narration_file() is None


def test_the_request_carries_everything_chosen(panel, state, tmp_path: Path):
    _give_narration(state)
    panel._mode.select("burn")
    panel._crop_choice.select("1:1")

    request = panel.build_request(tmp_path / "out.mp4")
    assert request.burn_subtitles
    assert request.crop is not None
    assert request.audio_path is not None and request.audio_path.exists()
    assert [s.text for s in request.segments] == ["Check the glucose."]
