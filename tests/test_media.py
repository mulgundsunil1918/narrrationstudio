"""Audio work with nothing installed.

The point of this module is that a new Mac needs no Homebrew, no Terminal and
no FFmpeg download before the app will run. So every test here hides any
``ffmpeg`` binary first: if one of them starts passing only because the machine
happens to have FFmpeg on it, the guarantee has quietly broken.
"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import pytest

from app.audio import media
from app.core.errors import AudioError

HAS_PYAV = media.have_pyav()
needs_pyav = pytest.mark.skipif(not HAS_PYAV, reason="PyAV is not installed")


@pytest.fixture
def no_ffmpeg_binary(no_installed_binaries):
    """A machine with no ffmpeg and no ffprobe. See conftest."""
    return no_installed_binaries


@pytest.fixture
def silent_video(tmp_path: Path) -> Path:
    """A video with a picture and no audio track, authored without any tool."""
    av = pytest.importorskip("av")
    path = tmp_path / "silent.mp4"
    with av.open(str(path), mode="w") as container:
        stream = container.add_stream("libx264", rate=5)
        stream.width, stream.height = 160, 120
        stream.pix_fmt = "yuv420p"
        for _ in range(10):
            frame = av.VideoFrame.from_ndarray(
                np.zeros((120, 160, 3), dtype=np.uint8), format="rgb24"
            )
            container.mux(stream.encode(frame))
        container.mux(stream.encode(None))
    return path


@pytest.fixture
def tone(tmp_path: Path) -> Path:
    """Six seconds of 24 kHz mono, written without any external tool."""
    path = tmp_path / "tone.wav"
    rate = 24_000
    samples = (
        np.sin(2 * np.pi * 440 * np.arange(rate * 6) / rate) * 0.5
    ).astype(np.float32)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes((samples * 32767).astype("<i2").tobytes())
    return path


# -- availability --------------------------------------------------------


@needs_pyav
def test_audio_is_available_without_any_binary(no_ffmpeg_binary):
    available, source = media.is_available()
    assert available
    assert "built in" in source


def test_unavailable_only_when_neither_exists(monkeypatch, no_ffmpeg_binary):
    monkeypatch.setattr(media, "_av", lambda: None)
    assert media.is_available() == (False, "")


# -- probing -------------------------------------------------------------


@needs_pyav
def test_probe_reads_duration_and_streams(no_ffmpeg_binary, tone: Path):
    info = media.probe(tone)
    assert info.readable
    assert info.has_audio
    assert not info.has_video
    assert info.duration_ms == pytest.approx(6000, abs=60)


@needs_pyav
def test_probe_of_a_missing_file_is_not_an_exception(no_ffmpeg_binary, tmp_path: Path):
    info = media.probe(tmp_path / "absent.mp4")
    assert not info.readable
    assert info.duration_ms == 0
    assert not info.has_audio


@needs_pyav
def test_probe_of_a_non_media_file_is_not_an_exception(
    no_ffmpeg_binary, tmp_path: Path
):
    junk = tmp_path / "notes.mp4"
    junk.write_text("this is not a video")
    assert not media.probe(junk).readable


# -- decoding ------------------------------------------------------------


@needs_pyav
def test_decode_produces_16k_mono(no_ffmpeg_binary, tone: Path, tmp_path: Path):
    out = media.decode_to_wav(tone, tmp_path / "out.wav", rate=16_000)

    with wave.open(str(out), "rb") as handle:
        assert handle.getframerate() == 16_000
        assert handle.getnchannels() == 1
        assert handle.getnframes() == pytest.approx(16_000 * 6, rel=0.02)


@needs_pyav
def test_decoding_something_with_no_audio_says_so(
    no_ffmpeg_binary, silent_video: Path, tmp_path: Path
):
    """The silent screen recording, caught with a sentence the user can act on."""
    info = media.probe(silent_video)
    assert info.has_audio is False
    assert info.has_video is True

    with pytest.raises(AudioError) as caught:
        media.decode_to_wav(silent_video, tmp_path / "out.wav")
    assert "no sound" in caught.value.message
    assert caught.value.suggestion


@needs_pyav
def test_decoding_junk_reports_a_decoding_problem(no_ffmpeg_binary, tmp_path: Path):
    junk = tmp_path / "broken.mp4"
    junk.write_text("not a video at all")
    with pytest.raises(AudioError) as caught:
        media.decode_to_wav(junk, tmp_path / "out.wav")
    assert caught.value.suggestion
    assert "brew" not in caught.value.suggestion.lower()


# -- time stretching -----------------------------------------------------


@needs_pyav
@pytest.mark.parametrize("factor", [1.25, 0.8, 1.5, 2.5, 0.4])
def test_time_stretch_hits_the_requested_length(no_ffmpeg_binary, factor: float):
    rate = 24_000
    audio = np.sin(2 * np.pi * 220 * np.arange(rate * 4) / rate).astype(np.float32)

    stretched = media.time_stretch(audio, factor, rate)
    assert len(stretched) == pytest.approx(len(audio) / factor, rel=0.01)


@needs_pyav
def test_chained_stages_are_used_beyond_the_filter_limit(no_ffmpeg_binary):
    """atempo caps at 2.0 per pass, so 4x has to be split into two."""
    from app.audio.timing import atempo_stages

    assert atempo_stages(4.0) == [2.0, 2.0]
    rate = 24_000
    audio = np.zeros(rate * 4, dtype=np.float32)
    assert len(media.time_stretch(audio, 4.0, rate)) == pytest.approx(rate, rel=0.02)


def test_no_op_stretch_returns_the_same_audio():
    audio = np.ones(1000, dtype=np.float32)
    assert media.time_stretch(audio, 1.0, 24_000) is audio


def test_stretching_empty_audio_is_not_an_error():
    empty = np.zeros(0, dtype=np.float32)
    assert media.time_stretch(empty, 1.5, 24_000).size == 0


@needs_pyav
def test_stretch_preserves_pitch(no_ffmpeg_binary):
    """atempo, not resampling: a 440 Hz tone must stay 440 Hz when sped up."""
    rate = 24_000
    audio = np.sin(2 * np.pi * 440 * np.arange(rate * 2) / rate).astype(np.float32)
    stretched = media.time_stretch(audio, 1.5, rate)

    spectrum = np.abs(np.fft.rfft(stretched * np.hanning(len(stretched))))
    peak_hz = np.fft.rfftfreq(len(stretched), 1 / rate)[np.argmax(spectrum)]
    assert peak_hz == pytest.approx(440, abs=15)


# -- encoding ------------------------------------------------------------


@needs_pyav
def test_mp3_is_written_and_readable(no_ffmpeg_binary, tone: Path, tmp_path: Path):
    out = media.encode_mp3(tmp_path / "out.mp3", tone)

    assert out.exists() and out.stat().st_size > 1000
    info = media.probe(out)
    assert info.has_audio
    assert info.duration_ms == pytest.approx(6000, abs=200)


# -- the whole point -----------------------------------------------------


@needs_pyav
def test_preflight_passes_with_no_ffmpeg_installed(no_ffmpeg_binary, tmp_path: Path):
    from app.core.models import Segment
    from app.core.preflight import run_preflight

    report = run_preflight(
        [Segment(0, 3000, "Hello.")], "kokoro", "af_heart", tmp_path / "o.wav", 3000
    )
    check = next(c for c in report.checks if c.key == "ffmpeg")
    assert check.passed
