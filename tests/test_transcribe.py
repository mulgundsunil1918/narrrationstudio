"""Transcription: the timeline it produces, and every way it can fail.

Two layers are tested separately. The pure parts — tidying Whisper's timings
into a caption list, deciding a file has no sound — run everywhere. The parts
that need FFmpeg or a downloaded model are marked and skipped when those are
absent, so CI stays useful without a gigabyte of dependencies.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
import wave
from pathlib import Path

import pytest

from app.core.errors import StudioError
from app.transcribe import (
    TranscribeRequest,
    Utterance,
    has_audio_track,
    media_duration_ms,
    to_segments,
    transcriber,
    transcriber_ids,
)
from app.transcribe.base import MIN_CAPTION_MS

HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="FFmpeg is not installed")


def _model_available(size: str = "tiny") -> bool:
    try:
        return size in transcriber("whisper").installed_models()
    except Exception:
        return False


needs_model = pytest.mark.skipif(
    not _model_available(), reason="no Whisper model has been downloaded"
)


# -- fixtures ------------------------------------------------------------


@pytest.fixture
def silent_wav(tmp_path: Path) -> Path:
    """A real WAV file containing nothing but silence."""
    path = tmp_path / "silence.wav"
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16_000)
        handle.writeframes(b"\x00\x00" * 16_000)
    return path


# -- the caption timeline ------------------------------------------------


def test_utterances_become_ascending_captions():
    segments = to_segments([
        Utterance(0, 2000, "First line."),
        Utterance(2000, 4500, "Second line."),
    ])
    assert [(s.start_ms, s.end_ms, s.text) for s in segments] == [
        (0, 2000, "First line."),
        (2000, 4500, "Second line."),
    ]


def test_overlapping_utterances_are_pushed_apart():
    """Whisper sometimes overlaps segments; the document model cannot take that."""
    segments = to_segments([
        Utterance(0, 3000, "One."),
        Utterance(2500, 5000, "Two."),
    ])
    assert segments[0].end_ms <= segments[1].start_ms
    assert segments[1].start_ms == 3000


def test_zero_length_utterance_gets_a_usable_window():
    segments = to_segments([Utterance(1000, 1000, "Hm.")])
    assert segments[0].duration_ms >= MIN_CAPTION_MS


def test_blank_utterances_are_dropped():
    segments = to_segments([
        Utterance(0, 1000, "   "),
        Utterance(1000, 2000, "Real words."),
        Utterance(2000, 3000, ""),
    ])
    assert [s.text for s in segments] == ["Real words."]


def test_out_of_order_utterances_are_sorted():
    segments = to_segments([
        Utterance(4000, 6000, "Later."),
        Utterance(0, 2000, "Earlier."),
    ])
    assert [s.text for s in segments] == ["Earlier.", "Later."]
    assert segments[0].start_ms < segments[1].start_ms


def test_no_utterances_is_an_empty_timeline_not_a_crash():
    assert to_segments([]) == []


# -- what the engine refuses to start ------------------------------------


def test_whisper_is_registered():
    assert "whisper" in transcriber_ids()


def test_missing_file_is_reported_before_any_work(tmp_path: Path):
    engine = transcriber("whisper")
    with pytest.raises(StudioError) as caught:
        engine.transcribe(TranscribeRequest(media_path=tmp_path / "nothing.mp4"))
    assert "could not be found" in caught.value.message
    assert caught.value.suggestion


@needs_ffmpeg
def test_silent_video_is_refused_with_a_reason(tmp_path: Path):
    """The silent screen recording is the file people try first."""
    video = tmp_path / "silent.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", "color=c=black:s=160x120:r=5", "-t", "2",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video)],
        check=True, capture_output=True,
    )
    assert has_audio_track(video) is False

    engine = transcriber("whisper")
    with pytest.raises(StudioError) as caught:
        engine.transcribe(TranscribeRequest(media_path=video))
    assert "no sound" in caught.value.message
    # It must point somewhere useful rather than just refusing.
    assert "script" in caught.value.suggestion


@needs_ffmpeg
def test_unreadable_file_reports_a_decoding_problem(tmp_path: Path):
    fake = tmp_path / "not-really.mp4"
    fake.write_text("this is not a video")

    engine = transcriber("whisper")
    with pytest.raises(StudioError) as caught:
        engine.transcribe(TranscribeRequest(media_path=fake))
    assert caught.value.suggestion


@needs_ffmpeg
def test_audio_track_detection(silent_wav: Path):
    assert has_audio_track(silent_wav) is True
    assert media_duration_ms(silent_wav) == pytest.approx(1000, abs=50)


def test_duration_of_a_missing_file_is_zero_not_an_exception(tmp_path: Path):
    assert media_duration_ms(tmp_path / "absent.wav") == 0


# -- the child process ---------------------------------------------------


def test_child_keeps_pytorch_out():
    """The isolation that stops the app being aborted by duplicate OpenMP.

    PyTorch (Kokoro) and CTranslate2 (Whisper) each bundle their own Intel
    OpenMP runtime, and two of those in one process kill it outright when a
    model loads — a native abort, with no exception to catch. The child must
    therefore never import torch, and this asserts the guard is in place rather
    than trusting a comment.
    """
    source = (
        Path(__file__).resolve().parents[1] / "app" / "transcribe" / "worker_main.py"
    ).read_text()
    guard = source.index('sys.modules.setdefault(_blocked, None)')
    import_site = source.index("import argparse")
    assert guard < import_site, "torch must be blocked before anything else is imported"

    result = subprocess.run(
        [
            "python", "-c",
            "import sys\n"
            "sys.argv = ['worker_main', '--help']\n"
            "import app.transcribe.worker_main as m\n"
            "print('torch' in sys.modules and sys.modules['torch'] is not None)",
        ],
        capture_output=True, text=True,
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    if result.returncode != 0:
        pytest.skip(f"child could not be imported here: {result.stderr[-200:]}")
    assert result.stdout.strip() == "False"


@needs_ffmpeg
@needs_model
def test_end_to_end_with_pytorch_already_loaded(tmp_path: Path):
    """The real configuration: Kokoro's torch is in memory before we transcribe.

    In-process this aborts the interpreter, so this test is the whole reason
    transcription runs as a child.
    """
    pytest.importorskip("torch")
    import torch  # noqa: F401

    speech = tmp_path / "speech.wav"
    # A tone is not speech, so assert on the mechanics rather than the words.
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", "sine=frequency=220:duration=3", "-ar", "16000", "-ac", "1",
         str(speech)],
        check=True, capture_output=True,
    )

    engine = transcriber("whisper")
    result = engine.transcribe(TranscribeRequest(media_path=speech, model_size="tiny"))
    assert result.model == "tiny"
    assert result.duration_ms > 0
    assert result.seconds_taken > 0


@needs_ffmpeg
@needs_model
def test_cancellation_stops_the_child(tmp_path: Path, monkeypatch):
    """Cancel must kill the process, not just stop reading from it.

    Checked by PID: ``pgrep`` matches its own arguments and reports phantoms.
    """
    speech = tmp_path / "long.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", "sine=frequency=220:duration=30", "-ar", "16000", "-ac", "1",
         str(speech)],
        check=True, capture_output=True,
    )

    spawned: list[int] = []
    real_popen = subprocess.Popen

    class Traced(real_popen):
        def __init__(self, command, *args, **kwargs):
            super().__init__(command, *args, **kwargs)
            if isinstance(command, list) and "app.transcribe.worker_main" in command:
                spawned.append(self.pid)

    monkeypatch.setattr(subprocess, "Popen", Traced)

    engine = transcriber("whisper")
    result = engine.transcribe(
        TranscribeRequest(media_path=speech, model_size="tiny"),
        should_cancel=lambda: True,
    )
    assert result.warnings and "stopped early" in result.warnings[0]
    assert spawned, "no transcription process was started"
    for pid in spawned:
        assert subprocess.run(
            ["ps", "-p", str(pid)], capture_output=True
        ).returncode != 0, f"process {pid} outlived its cancellation"


@needs_ffmpeg
@needs_model
def test_cancelling_before_any_output_does_not_wait(tmp_path: Path):
    """The worst moment to press Stop is during the first-time model download.

    Nothing is on stdout then, so a loop that only checks between lines would
    ignore the user until the model finished loading.
    """
    speech = tmp_path / "tone.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", "sine=frequency=220:duration=20", "-ar", "16000", "-ac", "1",
         str(speech)],
        check=True, capture_output=True,
    )

    engine = transcriber("whisper")
    started = time.monotonic()
    result = engine.transcribe(
        TranscribeRequest(media_path=speech, model_size="tiny"),
        should_cancel=lambda: True,
    )
    elapsed = time.monotonic() - started
    assert result.utterances == []
    assert elapsed < 3.0, f"cancelling took {elapsed:.1f}s"


def test_worker_emits_one_json_object_per_line():
    """The parent parses this stream line by line; it has to stay parseable."""
    from app.transcribe import worker_main

    written: list[str] = []
    worker_main.sys.stdout.write = written.append  # type: ignore[assignment]
    try:
        worker_main.emit({"event": "utterance", "text": "hello", "start": 0, "end": 1})
    finally:
        del worker_main.sys.stdout.write

    payload = "".join(written)
    assert payload.endswith("\n")
    assert json.loads(payload)["text"] == "hello"


# -- temporary files -----------------------------------------------------


@needs_ffmpeg
def test_extracted_audio_is_cleaned_up_after_a_failure(tmp_path: Path):
    """A failed run must not leave a WAV of the user's video behind."""
    from app.transcribe.base import extract_audio

    source = tmp_path / "tone.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=1", str(source)],
        check=True, capture_output=True,
    )
    extracted = extract_audio(source)
    assert extracted.exists()

    transcriber("whisper")._discard(extracted)
    assert not extracted.exists()
    assert not extracted.parent.exists()
