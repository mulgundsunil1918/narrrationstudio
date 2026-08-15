"""Negative testing: every failure must be visible and actionable.

Each test drives a real failure and asserts that what comes back is a
structured error with a code, a sentence for the user and a next step — never
an exception escaping to the console, and never a silent no-op.

Ordered to match the requested scenario list.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import numpy as np
import pytest

from app.core.errors import FileFormatError, UnsupportedFileError
from app.core.models import Segment
from app.config import APP_NAME
from app.core.preflight import run_preflight
from app.core.status import ErrorCode, OperationError, OperationState, Severity
from app.pipeline import CancellationToken, GenerationSettings, generate
from app.projects import store
from app.projects.store import ProjectData
from app.srt.parser import load as load_subtitles, parse_srt

SAMPLE = [
    Segment(0, 3000, "Welcome to the test narration."),
    Segment(3000, 6000, "This is the second section."),
]


def _engine_available() -> bool:
    """Whether the local speech engine can actually run here.

    CI installs the UI and audio libraries but not PyTorch, which is roughly a
    gigabyte. Tests that drive real synthesis skip there; everything that can be
    checked without it still runs on every push.
    """
    try:
        from app.tts.registry import engine as get_engine

        return get_engine("kokoro").is_available()[0]
    except Exception:
        return False


requires_engine = pytest.mark.skipif(
    not _engine_available(), reason="local speech engine is not installed"
)


def assert_actionable(error: OperationError) -> None:
    """Every user-facing error must say what, why and what next."""
    assert isinstance(error, OperationError)
    assert error.code in ErrorCode
    assert error.user_message and error.user_message.strip()
    assert error.recommended_action or error.reason, "no guidance for the user"
    # Raw Python must never be the headline.
    assert "Traceback" not in error.user_message
    assert not error.user_message.startswith(("Exception", "RuntimeError", "OSError"))
    # The technical report always exists for the disclosure panel.
    assert error.code.value in error.technical_report()


# -- 1. missing SRT -----------------------------------------------------


class TestMissingSubtitles:
    def test_absent_file_is_reported(self, tmp_path):
        with pytest.raises(Exception) as info:
            load_subtitles(tmp_path / "nope.srt")
        assert "nope.srt" in str(info.value) or "could not" in str(info.value).lower()

    def test_preflight_reports_no_captions(self, tmp_path):
        report = run_preflight([], "kokoro", "af_heart", tmp_path / "out.wav")
        assert not report.passed
        assert_actionable(report.first_error)
        assert report.first_error.code is ErrorCode.SRT_EMPTY

    def test_generate_refuses_without_captions(self):
        with pytest.raises(Exception) as info:
            generate([], GenerationSettings())
        assert "no subtitles" in str(info.value).lower()


# -- 2. corrupt SRT -----------------------------------------------------


class TestCorruptSubtitles:
    def test_binary_content_is_rejected(self, tmp_path):
        path = tmp_path / "bad.srt"
        path.write_bytes(b"\x00\x01\x02\xff\xfe" * 40)
        with pytest.raises(FileFormatError):
            load_subtitles(path)

    def test_prose_with_no_timings_is_rejected(self):
        with pytest.raises(FileFormatError) as info:
            parse_srt("just some prose\nwith no timings whatsoever")
        assert getattr(info.value, "suggestion", "")

    def test_empty_file_is_rejected(self, tmp_path):
        path = tmp_path / "empty.srt"
        path.write_text("   \n\n")
        with pytest.raises(FileFormatError):
            load_subtitles(path)

    def test_partially_corrupt_file_keeps_good_blocks_and_warns(self):
        content = (
            "1\n00:00:00,000 --> 00:00:02,000\nGood.\n\n"
            "2\nGARBAGE LINE\nBad.\n\n"
            "3\n00:00:03,000 --> 00:00:04,000\nAlso good.\n"
        )
        result = parse_srt(content)
        assert len(result.segments) == 2
        assert result.warnings, "dropped a block without telling anyone"


# -- 3. invalid timestamps ----------------------------------------------


class TestInvalidTimestamps:
    def test_malformed_timestamp_block_is_skipped_with_warning(self):
        content = "1\n99:99:99,999 --> 00:00:02,000\nBroken.\n\n" \
                  "2\n00:00:03,000 --> 00:00:04,000\nFine.\n"
        result = parse_srt(content)
        assert any("timestamp" in w.lower() for w in result.warnings)

    def test_overlapping_timings_block_generation(self, tmp_path):
        overlapping = [Segment(0, 5000, "One."), Segment(4000, 8000, "Two.")]
        report = run_preflight(overlapping, "kokoro", "af_heart", tmp_path / "o.wav")
        assert not report.passed
        error = report.first_error
        assert_actionable(error)
        assert error.code is ErrorCode.SRT_TIMESTAMP_OVERLAP

    def test_zero_duration_blocks_generation(self, tmp_path):
        report = run_preflight(
            [Segment(1000, 1000, "Impossible.")], "kokoro", "af_heart", tmp_path / "o.wav"
        )
        assert not report.passed
        assert_actionable(report.first_error)

    def test_generate_refuses_an_invalid_timeline(self):
        with pytest.raises(Exception) as info:
            generate([Segment(0, 5000, "A"), Segment(4000, 8000, "B")], GenerationSettings())
        assert "problem" in str(info.value).lower()


# -- 4 & 5. voice and model problems ------------------------------------


@requires_engine
class TestVoiceProblems:
    def test_unknown_voice_is_reported(self, tmp_path):
        report = run_preflight(SAMPLE, "kokoro", "not_a_real_voice", tmp_path / "o.wav")
        failures = [c for c in report.checks if c.key == "voice" and not c.passed]
        assert failures
        assert_actionable(failures[0].error)
        assert failures[0].error.code is ErrorCode.VOICE_NOT_FOUND

    def test_unknown_voice_offers_changing_it(self, tmp_path):
        report = run_preflight(SAMPLE, "kokoro", "not_a_real_voice", tmp_path / "o.wav")
        error = next(c.error for c in report.checks if c.key == "voice" and not c.passed)
        assert "change_voice" in error.actions

    def test_unknown_engine_is_reported(self, tmp_path):
        report = run_preflight(SAMPLE, "nonexistent_engine", "af_heart", tmp_path / "o.wav")
        assert not report.passed
        error = next(c.error for c in report.checks if c.key == "engine" and not c.passed)
        assert_actionable(error)
        assert error.code is ErrorCode.ENGINE_UNAVAILABLE

    def test_engine_failure_marks_segment_not_whole_project(self, monkeypatch):
        """A voice that cannot load must fail one segment, not vanish silently."""
        from app.tts.base import EngineUnavailable
        from app.tts.registry import engine as get_engine

        backend = get_engine("kokoro")
        monkeypatch.setattr(
            backend, "generate",
            lambda request: (_ for _ in ()).throw(
                EngineUnavailable("Voice model unavailable.", suggestion="Pick another voice.")
            ),
        )
        outcome = generate(SAMPLE, GenerationSettings(use_cache=False))
        assert outcome.failures, "a total engine failure produced no report"
        for failure in outcome.failures:
            assert_actionable(failure)
            assert failure.segment is not None
        assert outcome.state is OperationState.ERROR


# -- 6. FFmpeg unavailable ----------------------------------------------


class TestFFmpegMissing:
    """FFmpeg ships inside the app, so its absence from PATH is a non-event.

    These used to assert the opposite — that a missing ``ffmpeg`` executable
    failed pre-flight and sent the user to Homebrew. Requiring a Terminal
    command before the app would work at all was the most common reason it did
    not run on a new Mac, and the libraries are bundled now.
    """

    def test_no_ffmpeg_on_path_is_not_a_problem(self, no_installed_binaries, tmp_path):
        report = run_preflight(SAMPLE, "kokoro", "af_heart", tmp_path / "o.wav")

        check = next(c for c in report.checks if c.key == "ffmpeg")
        assert check.passed, "a missing ffmpeg binary must not block generation"
        assert "brew" not in (check.detail or "").lower()

    def test_preflight_fails_only_when_nothing_can_process_audio(
        self, monkeypatch, no_installed_binaries, tmp_path
    ):
        """The remaining failure mode is a broken install, not a missing tool.

        Both routes have to be gone: no bundled libraries *and* no binary.
        """
        from app.audio import media

        monkeypatch.setattr(media, "_av", lambda: None)
        report = run_preflight(SAMPLE, "kokoro", "af_heart", tmp_path / "o.wav")

        error = next(c.error for c in report.checks if c.key == "ffmpeg" and not c.passed)
        assert_actionable(error)
        assert error.code is ErrorCode.FFMPEG_NOT_FOUND
        assert not error.recoverable
        # Never tell someone to run a package manager to fix their own install.
        assert "brew" not in error.recommended_action.lower()
        assert "reinstall" in error.recommended_action.lower()

    @requires_engine
    def test_ffmpeg_failure_during_fitting_is_reported(self, monkeypatch):
        from app.core.errors import AudioError

        def explode(*_args, **_kwargs):
            raise AudioError(
                "FFmpeg could not adjust the speed of this narration group.",
                suggestion="Try regenerating the group.",
            )

        monkeypatch.setattr("app.pipeline.fit_audio", explode)
        outcome = generate(SAMPLE, GenerationSettings(use_cache=False))
        assert outcome.failures
        assert outcome.failures[0].code is ErrorCode.AUDIO_PROCESSING_FAILED
        assert_actionable(outcome.failures[0])


# -- 7. output not writable ---------------------------------------------


class TestOutputNotWritable:
    def test_read_only_folder_is_detected(self, tmp_path):
        folder = tmp_path / "locked"
        folder.mkdir()
        folder.chmod(stat.S_IREAD | stat.S_IEXEC)
        try:
            report = run_preflight(SAMPLE, "kokoro", "af_heart", folder / "out.wav")
            failures = [c for c in report.checks if c.key == "output" and not c.passed]
            if failures:  # root can write anywhere; skip the assertion there
                assert_actionable(failures[0].error)
                assert failures[0].error.code is ErrorCode.FILE_PERMISSION_DENIED
        finally:
            folder.chmod(stat.S_IRWXU)

    def test_project_save_to_read_only_folder_reports(self, tmp_path):
        folder = tmp_path / "ro"
        folder.mkdir()
        folder.chmod(stat.S_IREAD | stat.S_IEXEC)
        try:
            result = store.save(folder / "p.pediavid", ProjectData())
            if not result.success:
                assert_actionable(result.error)
                assert result.error.code in (
                    ErrorCode.FILE_PERMISSION_DENIED,
                    ErrorCode.PROJECT_SAVE_FAILED,
                )
        finally:
            folder.chmod(stat.S_IRWXU)


# -- 8. insufficient disk space -----------------------------------------


class TestDiskSpace:
    def test_low_disk_space_blocks_generation(self, monkeypatch, tmp_path):
        import shutil as shutil_module

        class Usage:
            total, used, free = 10**9, 10**9 - 1000, 1000

        monkeypatch.setattr(
            "app.core.preflight.shutil.disk_usage", lambda _p: Usage()
        )
        report = run_preflight(
            SAMPLE, "kokoro", "af_heart", tmp_path / "o.wav", timeline_ms=600_000
        )
        error = next(c.error for c in report.checks if c.key == "disk" and not c.passed)
        assert_actionable(error)
        assert error.code is ErrorCode.DISK_SPACE_LOW
        assert "space" in error.user_message.lower()

    def test_enospc_on_export_is_explained(self, tmp_path):
        from app.core.status import ErrorCode as EC

        error = OSError(28, "No space left on device")
        result = store.save.__wrapped__ if hasattr(store.save, "__wrapped__") else None
        # Exercised via the project saver, which classifies errno 28 explicitly.
        import app.projects.store as store_module

        def failing_write(*_a, **_k):
            raise error

        original = Path.write_text
        Path.write_text = failing_write
        try:
            outcome = store_module.save(tmp_path / "p.pediavid", ProjectData())
        finally:
            Path.write_text = original
        assert not outcome.success
        assert outcome.error.code is EC.DISK_SPACE_LOW
        assert_actionable(outcome.error)


# -- 9 & 10. TTS empty audio and segment failure -------------------------


@requires_engine
class TestTTSFailures:
    def test_empty_audio_is_a_reported_failure(self, monkeypatch):
        from app.tts.base import GenerationResult
        from app.tts.registry import engine as get_engine

        backend = get_engine("kokoro")
        monkeypatch.setattr(
            backend, "generate",
            lambda request: GenerationResult(
                audio=np.zeros(0, dtype=np.float32), sample_rate=24_000,
                duration_ms=0, engine="kokoro", voice=request.voice,
            ),
        )
        outcome = generate(SAMPLE, GenerationSettings(use_cache=False))
        assert outcome.failures
        assert outcome.failures[0].code is ErrorCode.TTS_EMPTY_AUDIO
        assert_actionable(outcome.failures[0])

    def test_one_bad_segment_does_not_lose_the_others(self, monkeypatch):
        from app.tts.base import GenerationResult
        from app.tts.registry import engine as get_engine

        calls = {"n": 0}

        def flaky(request):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("model exploded")
            audio = np.full(24_000, 0.2, dtype=np.float32)
            return GenerationResult(audio, 24_000, 1000, "kokoro", request.voice)

        backend = get_engine("kokoro")
        monkeypatch.setattr(backend, "generate", flaky)

        outcome = generate(SAMPLE, GenerationSettings(use_cache=False))
        assert len(outcome.failures) == 1
        assert outcome.completed_groups >= 1, "a single failure discarded good work"
        assert len(outcome.audio) > 0

    def test_failed_segment_records_which_one(self, monkeypatch):
        from app.tts.registry import engine as get_engine

        backend = get_engine("kokoro")
        monkeypatch.setattr(
            backend, "generate",
            lambda r: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        outcome = generate(SAMPLE, GenerationSettings(use_cache=False))
        assert outcome.failed_segments
        assert all(isinstance(n, int) for n in outcome.failed_segments)

    def test_retry_targets_only_the_failed_segment(self, monkeypatch):
        from app.tts.base import GenerationResult
        from app.tts.registry import engine as get_engine

        seen: list[str] = []

        def record(request):
            seen.append(request.text[:20])
            return GenerationResult(
                np.full(24_000, 0.1, dtype=np.float32), 24_000, 1000, "kokoro", request.voice
            )

        backend = get_engine("kokoro")
        monkeypatch.setattr(backend, "generate", record)
        generate(SAMPLE, GenerationSettings(use_cache=False), only_groups=[0])
        assert len(seen) == 1, "retry regenerated more than the failed segment"


# -- 11. cancellation ---------------------------------------------------


@requires_engine
class TestCancellation:
    def test_cancelling_stops_work_and_reports(self, monkeypatch):
        from app.tts.base import GenerationResult
        from app.tts.registry import engine as get_engine

        token = CancellationToken()
        calls = {"n": 0}

        def counting(request):
            calls["n"] += 1
            token.cancel()  # cancel after the first group
            return GenerationResult(
                np.full(24_000, 0.1, dtype=np.float32), 24_000, 1000, "kokoro", request.voice
            )

        backend = get_engine("kokoro")
        monkeypatch.setattr(backend, "generate", counting)

        outcome = generate(SAMPLE, GenerationSettings(use_cache=False), token=token)
        assert outcome.cancelled
        assert outcome.state is OperationState.CANCELLED
        assert calls["n"] < len(outcome.plan) + 1

    def test_cancelling_preserves_completed_work(self, monkeypatch):
        from app.tts.base import GenerationResult
        from app.tts.registry import engine as get_engine

        token = CancellationToken()

        def once(request):
            token.cancel()
            return GenerationResult(
                np.full(24_000, 0.3, dtype=np.float32), 24_000, 1000, "kokoro", request.voice
            )

        backend = get_engine("kokoro")
        monkeypatch.setattr(backend, "generate", once)
        outcome = generate(SAMPLE, GenerationSettings(use_cache=False), token=token)
        assert outcome.completed_groups >= 1
        assert len(outcome.audio) > 0

    def test_cancel_before_start_produces_no_audio_but_a_clear_state(self, monkeypatch):
        token = CancellationToken()
        token.cancel()
        outcome = generate(SAMPLE, GenerationSettings(use_cache=False), token=token)
        assert outcome.cancelled
        assert outcome.completed_groups == 0


# -- 12. closing during generation --------------------------------------


class TestCloseDuringGeneration:
    def test_token_is_cooperative_so_close_can_stop_it(self):
        token = CancellationToken()
        assert not token.cancelled
        token.cancel()
        assert token.cancelled

    def test_state_machine_exposes_generating_so_close_can_warn(self):
        assert OperationState.GENERATING.is_busy
        assert not OperationState.GENERATING.is_terminal
        assert OperationState.CANCELLED.is_terminal


# -- 13. project problems ------------------------------------------------


class TestProjectFailures:
    def test_missing_project_is_reported(self, tmp_path):
        result = store.load(tmp_path / "gone.pediavid")
        assert not result.success
        assert_actionable(result.error)
        assert result.error.code is ErrorCode.FILE_NOT_FOUND

    def test_corrupt_project_is_reported(self, tmp_path):
        path = tmp_path / "bad.pediavid"
        path.write_text("{not json at all")
        result = store.load(path)
        assert not result.success
        assert_actionable(result.error)
        assert result.error.code is ErrorCode.PROJECT_LOAD_FAILED

    def test_wrong_file_type_is_reported(self, tmp_path):
        path = tmp_path / "other.pediavid"
        path.write_text('{"format": "something-else"}')
        result = store.load(path)
        assert not result.success
        assert "is not a" in result.error.user_message
        assert APP_NAME in result.error.user_message

    def test_duplicate_creates_a_new_file(self, tmp_path):
        path = tmp_path / "p.pediavid"
        store.save(path, ProjectData(name="Original"))
        result = store.duplicate(path)
        assert result.success
        assert result.value.exists()
        assert result.value != path

    def test_duplicate_twice_does_not_collide(self, tmp_path):
        path = tmp_path / "p.pediavid"
        store.save(path, ProjectData(name="Original"))
        first = store.duplicate(path).unwrap()
        second = store.duplicate(path).unwrap()
        assert first != second

    def test_duplicating_a_missing_project_is_reported(self, tmp_path):
        result = store.duplicate(tmp_path / "absent.pediavid")
        assert not result.success
        assert_actionable(result.error)

    def test_project_round_trip_preserves_captions(self, tmp_path):
        path = tmp_path / "p.pediavid"
        data = ProjectData(name="Test", captions=store.segments_to_payload(SAMPLE))
        assert store.save(path, data).success
        loaded = store.load(path).unwrap()
        restored = store.payload_to_segments(loaded.captions)
        assert [(s.start_ms, s.end_ms, s.text) for s in restored] == [
            (s.start_ms, s.end_ms, s.text) for s in SAMPLE
        ]

    def test_unreadable_caption_is_skipped_not_fatal(self):
        payload = [
            {"start_ms": 0, "end_ms": 1000, "text": "Fine."},
            {"start_ms": "not a number", "end_ms": 2000, "text": "Broken."},
        ]
        restored = store.payload_to_segments(payload)
        assert len(restored) == 1


# -- 14. unsupported media ----------------------------------------------


class TestUnsupportedMedia:
    def test_unsupported_extension_is_rejected_by_the_importer(self, tmp_path):
        path = tmp_path / "clip.xyz"
        path.write_bytes(b"\x00\x01")
        with pytest.raises(UnsupportedFileError):
            load_subtitles(path)

    def test_dropzone_classifies_known_types(self):
        from app.ui.widgets.dropzone import classify

        assert classify(Path("a.srt")) == "subtitles"
        assert classify(Path("a.mp4")) == "video"
        assert classify(Path("a.wav")) == "audio"
        assert classify(Path("a.pediavid")) == "project"

    def test_dropzone_rejects_unknown_types(self):
        from app.ui.widgets.dropzone import classify

        for name in ("a.exe", "a.zip", "a.docx", "a.avi.part", "a"):
            assert classify(Path(name)) == "unsupported"

    def test_oversized_file_is_refused_before_reading(self, tmp_path):
        path = tmp_path / "huge.srt"
        path.write_text("1\n00:00:00,000 --> 00:00:01,000\nHi.\n")
        original = Path.stat

        class BigStat:
            st_size = 200 * 1024 * 1024

        Path.stat = lambda self, **_k: BigStat() if self == path else original(self)
        try:
            with pytest.raises(FileFormatError) as info:
                load_subtitles(path)
            assert "too large" in str(info.value)
            assert "MB" in info.value.reason
        finally:
            Path.stat = original


# -- cross-cutting -------------------------------------------------------


class TestNoSilentFailures:
    def test_every_error_code_has_actions(self):
        for code in ErrorCode:
            error = OperationError(code, "Something failed.")
            assert error.actions, f"{code.value} offers the user nothing"

    def test_capture_preserves_the_traceback_for_the_details_panel(self):
        from app.core.status import capture

        try:
            raise ValueError("inner detail")
        except ValueError as exc:
            error = capture(exc, ErrorCode.UNKNOWN_ERROR, user_message="It broke.")
        assert "ValueError" in error.details
        assert "inner detail" in error.details
        assert error.user_message == "It broke."

    def test_warnings_are_distinct_from_errors(self):
        from app.core.status import warning

        note = warning(ErrorCode.TTS_GENERATION_FAILED, "Segment ran fast.")
        assert note.severity is Severity.WARNING

    def test_terminal_states_are_terminal(self):
        for state in (
            OperationState.COMPLETED, OperationState.ERROR,
            OperationState.CANCELLED, OperationState.WARNING,
        ):
            assert state.is_terminal
            assert not state.is_busy
