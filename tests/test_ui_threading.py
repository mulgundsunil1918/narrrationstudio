"""Worker results must arrive on the UI thread.

A signal connected to a lambda has no thread affinity, so Qt runs the handler on
the worker thread — where touching a QMediaPlayer or a QTimer fails with only a
console warning. That is how voice preview ended up stuck on "Loading…" with no
sound and no error. These tests pin the contract so it cannot regress.

Run headless via the offscreen platform; nothing here touches an audio device.
"""

from __future__ import annotations

import os
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication, QDialog

from app.tts.base import GenerationResult
from app.tts.registry import engine as get_engine


@pytest.fixture(scope="module")
def qt_app():
    application = QApplication.instance() or QApplication([])
    yield application


@pytest.fixture(scope="module")
def _window(qt_app):
    """One window for the whole module.

    Building and tearing down a QMainWindow per test crashes the interpreter
    during pytest's teardown, so the window is shared and reset between tests.
    """
    from app.ui.main_window import MainWindow
    from app.ui.state import AppState
    from app.ui.theme import Appearance, apply_theme
    from app.ui.widgets.error_dialog import ErrorDialog

    apply_theme(qt_app, Appearance.DARK)
    errors: list = []
    ErrorDialog.exec = lambda self: (
        errors.append(self._error), QDialog.DialogCode.Rejected
    )[1]

    main = MainWindow(AppState())
    main.go("voice")
    qt_app.processEvents()
    main._captured_errors = errors
    # Never reach a real audio device from a test.
    main._played = {}
    main.review.play_file = lambda p: main._played.update(
        path=p, thread=threading.get_ident()
    )
    yield main

    # Shut down explicitly. Letting Python collect a QMainWindow that owns a
    # QMediaPlayer and worker threads at interpreter exit crashes the process.
    from app.ui.workers import wait_for_threads

    main.review.stop()
    wait_for_threads()
    main.hide()
    main.setParent(None)
    qt_app.processEvents()
    main.deleteLater()
    qt_app.processEvents()


@pytest.fixture
def window(_window, qt_app):
    _window._captured_errors.clear()
    _window._played.clear()
    _window._previewing = ""
    _window.voice.clear_preview_busy()
    qt_app.processEvents()
    yield _window
    # Let any in-flight worker finish so it cannot land during the next test.
    pump(qt_app, lambda: _window._previewing == "", timeout_ms=8000)


def pump(application, predicate, timeout_ms: int = 8000) -> None:
    loop = QEventLoop()
    ticker = QTimer()
    ticker.timeout.connect(lambda: loop.quit() if predicate() else None)
    ticker.start(25)
    guard = QTimer()
    guard.setSingleShot(True)
    guard.timeout.connect(loop.quit)
    guard.start(timeout_ms)
    loop.exec()


def stub_engine(monkeypatch, recorder: dict):
    def fake(request):
        recorder["thread"] = threading.get_ident()
        return GenerationResult(
            np.full(2400, 0.2, dtype=np.float32), 24_000, 100, "kokoro", request.voice
        )

    monkeypatch.setattr(get_engine("kokoro"), "generate", fake)


class TestPreviewThreading:
    def test_worker_runs_off_the_ui_thread(self, qt_app, window, monkeypatch):
        recorder: dict = {}
        stub_engine(monkeypatch, recorder)
        window.preview_voice("af_heart")
        pump(qt_app, lambda: bool(window._played) or bool(window._captured_errors))
        assert recorder["thread"] != threading.get_ident()

    def test_result_is_delivered_on_the_ui_thread(self, qt_app, window, monkeypatch):
        stub_engine(monkeypatch, {})
        window.preview_voice("af_heart")
        pump(qt_app, lambda: bool(window._played) or bool(window._captured_errors))
        assert not window._captured_errors
        assert window._played["thread"] == threading.get_ident()

    def test_playback_is_actually_invoked(self, qt_app, window, monkeypatch):
        stub_engine(monkeypatch, {})
        window.preview_voice("af_heart")
        pump(qt_app, lambda: bool(window._played) or bool(window._captured_errors))
        assert "path" in window._played


class TestPreviewNeverSticks:
    def test_busy_state_clears_on_success(self, qt_app, window, monkeypatch):
        stub_engine(monkeypatch, {})
        window.preview_voice("af_heart")
        pump(qt_app, lambda: bool(window._played) or bool(window._captured_errors))
        assert window._previewing == ""
        assert all(
            "Loading" not in card._preview_button.text()
            for card in window.voice._cards
        )

    def test_busy_state_clears_on_failure(self, qt_app, window, monkeypatch):
        monkeypatch.setattr(
            get_engine("kokoro"),
            "generate",
            lambda r: (_ for _ in ()).throw(RuntimeError("model gone")),
        )
        window.preview_voice("af_heart")
        pump(qt_app, lambda: bool(window._captured_errors))
        assert window._captured_errors
        assert window._previewing == ""
        assert all(
            "Loading" not in card._preview_button.text()
            for card in window.voice._cards
        )

    def test_timeout_reports_and_clears(self, qt_app, window):
        window._previewing = "af_sky"
        window.voice.set_preview_busy("af_sky", True)
        window._on_preview_timeout()
        assert window._captured_errors
        assert window._captured_errors[-1].code.value == "TTS_TIMEOUT"
        assert window._previewing == ""

    def test_concurrent_preview_is_refused_not_queued(self, qt_app, window, monkeypatch):
        stub_engine(monkeypatch, {})
        window.preview_voice("af_heart")
        window.preview_voice("af_bella")   # must not replace the first
        assert window._previewing == "af_heart"
        pump(qt_app, lambda: bool(window._played) or bool(window._captured_errors))
        assert window._previewing == ""

    def test_a_second_preview_works_after_the_first(self, qt_app, window, monkeypatch):
        stub_engine(monkeypatch, {})
        window.preview_voice("af_heart")
        pump(qt_app, lambda: bool(window._played))
        window._played.clear()
        window.preview_voice("af_bella")
        pump(qt_app, lambda: bool(window._played) or bool(window._captured_errors))
        assert "path" in window._played


class TestEngineSharing:
    def test_pipeline_is_built_once_under_concurrency(self, monkeypatch):
        """Two threads asking at once must share one pipeline, not build two."""
        engine = get_engine("kokoro")
        engine._pipelines.clear()
        builds = {"n": 0}

        class FakePipeline:
            def __init__(self, lang_code):
                builds["n"] += 1

        import kokoro

        monkeypatch.setattr(kokoro, "KPipeline", FakePipeline)

        errors: list = []

        def ask():
            try:
                engine._pipeline("a")
            except Exception as exc:  # surfaced, never swallowed
                errors.append(exc)

        threads = [threading.Thread(target=ask) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors
        assert builds["n"] == 1, f"built the model {builds['n']} times"
        engine._pipelines.clear()
