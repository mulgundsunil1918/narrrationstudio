"""The main window: navigation, the workflow rail, and global wiring.

Every screen is built here and talks only to :class:`AppState`. All errors from
anywhere in the app arrive on ``state.error_raised`` and are shown by exactly
one handler, so a failure cannot reach the user as silence.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QCloseEvent, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from app.config import APP_NAME, PROJECT_SUFFIX, VOICE_PREVIEW_TEXT
from app.core.errors import StudioError
from app.core.status import ErrorCode, OperationError, OperationState, capture
from app.projects import store
from app.srt.parser import load as load_subtitles
from app.ui.screens.enhance import EnhanceDialog
from app.ui.screens.export import ExportScreen
from app.ui.screens.generate import GenerateScreen
from app.ui.screens.home import HomeScreen
from app.ui.screens.narration import NarrationScreen
from app.ui.screens.review import ReviewScreen
from app.ui.screens.script import ScriptScreen
from app.ui.screens.settings_screen import SettingsScreen
from app.ui.screens.voice import VoiceScreen
from app.ui.state import AppState
from app.ui.theme import Appearance, apply_theme, palette
from app.ui.widgets.common import Divider, Pill, Toast, caption, label, muted
from app.ui.widgets import workflow
from app.ui.widgets.error_dialog import show_error
from app.ui.widgets.workflow import StepFooter, StepRail
from app.ui.workers import PreviewWorker, run_in_thread, wait_for_threads

logger = logging.getLogger(__name__)

NAV = [
    ("home", "Home"),
    ("script", "Script"),
    ("voice", "Voice"),
    ("narration", "Narration"),
    ("generate", "Generate"),
    ("review", "Preview"),
    ("export", "Export"),
    ("settings", "Settings"),
]

#: Screens that need a script loaded before they mean anything.
NEEDS_SCRIPT = {"script", "narration", "generate", "review", "export"}

#: Generous enough to cover a first-time voice download, short enough that a
#: hung preview reports itself rather than sitting on "Loading…" indefinitely.
PREVIEW_TIMEOUT_MS = 120_000


class MainWindow(QMainWindow):
    def __init__(self, state: AppState) -> None:
        super().__init__()
        self.state = state
        self._preview_thread = None
        self._previewing = ""
        self._current = "home"
        self._visited: set[str] = set()
        self._closing = False

        self._preview_timeout = QTimer(self)
        self._preview_timeout.setSingleShot(True)
        self._preview_timeout.timeout.connect(self._on_preview_timeout)

        self.setWindowTitle(APP_NAME)
        self.resize(1320, 880)
        self.setMinimumSize(QSize(1060, 700))

        central = QWidget()
        central.setObjectName("Workspace")
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._build_sidebar())
        layout.addWidget(self._build_workspace(), 1)
        self.setCentralWidget(central)

        self._toast = Toast(central)
        self._build_menus()
        self._connect_state()
        self.go("home")
        QTimer.singleShot(400, self._offer_recovery)

    # -- chrome ----------------------------------------------------------

    def _build_sidebar(self) -> QWidget:
        panel = QWidget()
        panel.setObjectName("Sidebar")
        panel.setFixedWidth(216)
        column = QVBoxLayout(panel)
        column.setContentsMargins(14, 20, 14, 16)
        column.setSpacing(4)

        first, _, rest = APP_NAME.partition(" ")
        brand = label(first, "Heading")
        brand.setContentsMargins(8, 0, 0, 0)
        column.addWidget(brand)
        sub = caption(rest.upper())
        sub.setContentsMargins(8, 0, 0, 0)
        column.addWidget(sub)
        column.addSpacing(18)

        self._nav_buttons: dict[str, QPushButton] = {}
        for key, text in NAV:
            if key == "settings":
                column.addStretch(1)
                column.addWidget(Divider())
                column.addSpacing(6)
            button = QPushButton(text)
            button.setObjectName("Nav")
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _c=False, k=key: self.go(k))
            column.addWidget(button)
            self._nav_buttons[key] = button

        column.addSpacing(10)
        self._project_pill = Pill("No project", "neutral")
        column.addWidget(self._project_pill)
        self._save_state = caption("")
        self._save_state.setContentsMargins(8, 2, 0, 0)
        column.addWidget(self._save_state)
        return panel

    def _build_workspace(self) -> QWidget:
        """The stack, wrapped in the step rail and the Back/Continue footer."""
        holder = QWidget()
        holder.setObjectName("Workspace")
        column = QVBoxLayout(holder)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)

        self._rail = StepRail()
        self._rail.step_clicked.connect(self.go)
        column.addWidget(self._rail)

        column.addWidget(self._build_stack(), 1)

        self._footer = StepFooter()
        self._footer.back.connect(self.go_back)
        self._footer.forward.connect(self.go_forward)
        column.addWidget(self._footer)
        return holder

    def _build_stack(self) -> QWidget:
        self._stack = QStackedWidget()
        self.home = HomeScreen(self.state)
        self.script = ScriptScreen(self.state)
        self.voice = VoiceScreen(self.state)
        self.narration = NarrationScreen(self.state)
        self.generate = GenerateScreen(self.state)
        self.review = ReviewScreen(self.state)
        self.export = ExportScreen(self.state)
        self.settings = SettingsScreen(self.state)

        self._screens = {
            "home": self.home,
            "script": self.script,
            "voice": self.voice,
            "narration": self.narration,
            "generate": self.generate,
            "review": self.review,
            "export": self.export,
            "settings": self.settings,
        }
        for screen in self._screens.values():
            self._stack.addWidget(screen)

        self.home.open_file.connect(self.import_file)
        self.home.open_project.connect(self.open_project)
        self.home.rejected.connect(self._on_rejected_file)
        self.script.request_enhance.connect(self.show_enhance)
        self.voice.preview_requested.connect(self.preview_voice)
        self.generate.finished.connect(lambda _o: self.go("review"))
        self.generate.change_voice_requested.connect(lambda: self.go("voice"))
        self.review.export_requested.connect(lambda: self.go("export"))
        self.review.caption_active.connect(self.script.set_active)
        self.settings.appearance_changed.connect(self.set_appearance)
        return self._stack

    def _build_menus(self) -> None:
        bar = self.menuBar()

        file_menu = bar.addMenu("File")
        self._add_action(file_menu, "New Project", QKeySequence.StandardKey.New, self.new_project)
        self._add_action(file_menu, "Open Project…", QKeySequence.StandardKey.Open, self.choose_project)
        self._add_action(file_menu, "Import Subtitles…", "Ctrl+I", self.choose_subtitles)
        file_menu.addSeparator()
        self._add_action(file_menu, "Save", QKeySequence.StandardKey.Save, self.save_project)
        self._add_action(file_menu, "Save As…", QKeySequence.StandardKey.SaveAs, self.save_project_as)

        edit_menu = bar.addMenu("Edit")
        self._add_action(edit_menu, "Undo", QKeySequence.StandardKey.Undo, self.state.document.undo)
        self._add_action(edit_menu, "Redo", QKeySequence.StandardKey.Redo, self.state.document.redo)
        edit_menu.addSeparator()
        self._add_action(edit_menu, "Enhance Script…", "Ctrl+Shift+E", self.show_enhance)

        generate_menu = bar.addMenu("Generate")
        self._add_action(
            generate_menu, "Generate Narration", "Ctrl+Return", self._generate_now
        )
        self._add_action(generate_menu, "Play / Pause", "Space", self.review.toggle_play)

        view_menu = bar.addMenu("View")
        self._advanced_action = QAction("Advanced Mode", self)
        self._advanced_action.setCheckable(True)
        self._advanced_action.setShortcut("Ctrl+Shift+A")
        self._advanced_action.toggled.connect(self.state.set_advanced)
        view_menu.addAction(self._advanced_action)

    def _add_action(self, menu, text: str, shortcut, slot) -> QAction:
        action = QAction(text, self)
        if shortcut:
            action.setShortcut(shortcut if isinstance(shortcut, str) else QKeySequence(shortcut))
        action.triggered.connect(slot)
        menu.addAction(action)
        self.addAction(action)
        return action

    # -- state wiring ----------------------------------------------------

    def _connect_state(self) -> None:
        self.state.error_raised.connect(self.show_error)
        self.state.notify.connect(self._toast_message)
        self.state.project_changed.connect(self._refresh_chrome)
        self.state.dirty_changed.connect(self._on_dirty)
        self.state.project_saved.connect(self._on_saved)
        self.state.advanced_changed.connect(self._advanced_action.setChecked)
        self.state.generation_finished.connect(lambda _o: self._refresh_workflow())
        self.state.state_changed.connect(lambda _s: self._refresh_workflow())
        self._refresh_chrome()

    def _refresh_chrome(self) -> None:
        has = self.state.has_captions
        for key, button in self._nav_buttons.items():
            if key in NEEDS_SCRIPT:
                button.setEnabled(has)
        if has:
            name = self.state.project.name or "Untitled"
            self._project_pill.set_status(name[:26], "info")
        else:
            self._project_pill.set_status("No project", "neutral")
        self._refresh_workflow()

    def _on_dirty(self, dirty: bool) -> None:
        self._save_state.setText("Unsaved changes" if dirty else "")

    def _on_saved(self, path: Path) -> None:
        self._save_state.setText("Saved ✓")
        QTimer.singleShot(2500, lambda: self._save_state.setText(""))

    def _toast_message(self, message: str, kind: str) -> None:
        self._toast.show_message(message, kind)

    # -- navigation ------------------------------------------------------

    def go(self, key: str) -> None:
        if key in NEEDS_SCRIPT and not self.state.has_captions:
            self._toast_message("Import a subtitle file first.", "warning")
            key = "home"
        screen = self._screens.get(key)
        if screen is None:
            return
        self._current = key
        self._visited.add(key)
        self._stack.setCurrentWidget(screen)
        for name, button in self._nav_buttons.items():
            button.setChecked(name == key)
        if key == "voice":
            self.voice.reload()
        elif key == "narration":
            self.narration.refresh()
        elif key == "generate":
            self.generate.run_checks()
        elif key == "export":
            self.export.refresh()
        self._refresh_workflow()

    # -- guided workflow -------------------------------------------------

    def _completed_steps(self) -> set[str]:
        """Steps the user has actually finished.

        A step counts as done once they have moved past it, or once it produced
        a real artifact. Marking a step done merely because it holds a default
        value would put a tick on work nobody has looked at.
        """
        done: set[str] = set()
        if self._current in workflow.BY_KEY:
            position = workflow.ORDER.index(self._current)
            done.update(
                key
                for key in workflow.ORDER[:position]
                if key in self._visited
            )
        if self.state.outcome is not None and not self.state.outcome.failures:
            done.add("generate")
        if self.state.generated_path is not None:
            done.add("export")
        return done

    def _can_advance(self, key: str) -> tuple[bool, str]:
        """Whether Continue should work here, and why not if it shouldn't."""
        if key == "review" and self.state.outcome is None:
            return False, "Generate the narration first"
        return True, ""

    def _next_label_for(self, key: str) -> str:
        """Override the Continue label where the step's own action is the point.

        On Generate with nothing rendered yet, telling the user to generate
        while the primary button sits greyed out is a dead end. The button
        performs the action instead.
        """
        if key == "generate" and self.state.outcome is None:
            return "▶  Generate Narration"
        if key == "generate" and self.state.is_busy:
            return "Generating…"
        return ""

    def _refresh_workflow(self) -> None:
        in_flow = self._current in workflow.BY_KEY
        self._rail.setVisible(in_flow)
        self._footer.setVisible(in_flow)
        if not in_flow:
            return

        enabled = set(workflow.ORDER) if self.state.has_captions else set()
        self._rail.set_state(self._current, self._completed_steps(), enabled)

        step = workflow.BY_KEY[self._current]
        can, reason = self._can_advance(self._current)
        self._footer.set_step(
            step,
            can_advance=can and not self.state.is_busy,
            blocked_reason=reason,
            is_first=workflow.ORDER.index(self._current) == 0,
            next_label=self._next_label_for(self._current),
        )

    def go_forward(self) -> None:
        """Continue: advance one step, doing the step's work where that's the point."""
        if self._current == "generate" and self.state.outcome is None:
            self.generate.start()
            return
        if self._current == "export":
            self._toast_message("All done. Your files are saved.", "success")
            return
        position = workflow.ORDER.index(self._current)
        if position + 1 < len(workflow.ORDER):
            self.go(workflow.ORDER[position + 1])

    def go_back(self) -> None:
        if self._current not in workflow.BY_KEY:
            return
        position = workflow.ORDER.index(self._current)
        if position == 0:
            self.go("home")
            return
        self.go(workflow.ORDER[position - 1])

    # -- import ----------------------------------------------------------

    def import_file(self, path: Path, kind: str) -> None:
        if kind == "subtitles":
            self.import_subtitles(path)
        elif kind in ("video", "audio"):
            self._import_media(path, kind)

    def import_subtitles(self, path: Path) -> None:
        try:
            parsed = load_subtitles(path)
        except StudioError as exc:
            self.show_error(
                OperationError(
                    ErrorCode.SRT_INVALID,
                    getattr(exc, "message", str(exc)),
                    reason=getattr(exc, "reason", "")
                    or f"“{path.name}” could not be read as subtitles.",
                    recommended_action=getattr(exc, "suggestion", "")
                    or "Choose a different subtitle file.",
                    details=getattr(exc, "detail", "") or "",
                    operation="import_subtitles",
                )
            )
            return
        except Exception as exc:
            self.show_error(
                capture(
                    exc,
                    ErrorCode.SRT_INVALID,
                    user_message=f"“{path.name}” could not be imported.",
                    recommended_action="Choose a different subtitle file.",
                    operation="import_subtitles",
                )
            )
            return

        self.state.load_segments(parsed.segments, path)
        message = f"Imported {len(parsed.segments)} subtitles from {path.name}"
        if parsed.warnings:
            message += f" · {len(parsed.warnings)} note(s)"
            for warning in parsed.warnings[:3]:
                logger.warning("%s: %s", path.name, warning)
        self._toast_message(message, "success")
        self.go("script")

    def _import_media(self, path: Path, kind: str) -> None:
        """Media import is accepted for context; narration still needs subtitles."""
        self.state.media_path = path
        self._toast_message(
            f"Linked {path.name}. Now import the subtitle file for this "
            f"{'video' if kind == 'video' else 'audio'}.",
            "info",
        )

    def _on_rejected_file(self, path: Path, reason: str) -> None:
        self.show_error(
            OperationError(
                ErrorCode.SRT_UNSUPPORTED,
                f"“{path.name}” cannot be opened.",
                reason=reason,
                recommended_action="Choose an SRT, TXT, MD, MP4, MOV, WAV or MP3 file.",
                operation="import",
            )
        )

    def choose_subtitles(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Import Subtitles", str(Path.home()),
            "Subtitles (*.srt *.txt *.md);;All files (*)",
        )
        if path:
            self.import_subtitles(Path(path))

    # -- projects --------------------------------------------------------

    def new_project(self) -> None:
        if not self._confirm_discard():
            return
        self.state.new_project()
        self._visited.clear()
        self._toast_message("New project", "info")
        self.go("home")

    def choose_project(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Project", str(Path.home()),
            f"{APP_NAME} projects (*{PROJECT_SUFFIX})",
        )
        if path:
            self.open_project(Path(path))

    def open_project(self, path: Path) -> None:
        if not self._confirm_discard():
            return
        result = self.state.open_project(path)
        if not result.success and result.error:
            self.show_error(result.error)
            return
        self._toast_message(f"Opened {path.stem}", "success")
        self.go("script")

    def save_project(self) -> None:
        if self.state.project_path is None:
            self.save_project_as()
            return
        result = self.state.save()
        if not result.success and result.error:
            self.show_error(result.error)

    def save_project_as(self) -> None:
        if not self.state.has_captions:
            self._toast_message("There is nothing to save yet.", "warning")
            return
        suggested = str(
            Path.home() / "Desktop" / f"{self.state.project.name or 'Narration'}{PROJECT_SUFFIX}"
        )
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Project As", suggested, f"{APP_NAME} projects (*{PROJECT_SUFFIX})"
        )
        if not path:
            return
        result = self.state.save(Path(path))
        if not result.success and result.error:
            self.show_error(result.error)

    def _offer_recovery(self) -> None:
        path = store.pending_recovery()
        if path is None:
            return
        answer = QMessageBox.question(
            self,
            "Recover unsaved project",
            f"{APP_NAME} did not close normally last time.\n\n"
            "Would you like to recover the project you were working on?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            result = self.state.open_project(path)
            if result.success:
                self.state.project_path = None  # force Save As for the recovered copy
                self._toast_message("Recovered unsaved project", "success")
                self.go("script")
            elif result.error:
                self.show_error(result.error)
        else:
            store.clear_recovery()

    # -- actions ---------------------------------------------------------

    def show_enhance(self) -> None:
        if not self.state.has_captions:
            self._toast_message("Import a script first.", "warning")
            return
        EnhanceDialog(self.state, self).exec()

    def _generate_now(self) -> None:
        self.go("generate")
        self.generate.start()

    def preview_voice(self, voice_id: str) -> None:
        if self._previewing:
            self._toast_message(
                f"Still preparing “{self._previewing}” — one preview at a time.",
                "warning",
            )
            return

        self._previewing = voice_id
        self.voice.set_preview_busy(voice_id, True)
        worker = PreviewWorker(
            self.state.voice.engine, voice_id, VOICE_PREVIEW_TEXT, self.state.voice.speed
        )
        # Delivered on the UI thread, explicitly. A lambda here would have no
        # thread affinity and Qt would run the handler on the worker thread,
        # where touching the media player or a timer fails silently — which is
        # what left preview buttons stuck on "Loading…" with no sound.
        worker.finished.connect(
            self._on_preview_ready, Qt.ConnectionType.QueuedConnection
        )
        worker.failed.connect(
            self._on_preview_failed, Qt.ConnectionType.QueuedConnection
        )
        self._preview_thread = run_in_thread(worker, self)

        # A preview that never returns must not leave a button saying "Loading…"
        # for the rest of the session.
        self._preview_timeout.start(PREVIEW_TIMEOUT_MS)

    def _finish_preview(self) -> None:
        self._preview_timeout.stop()
        if self._previewing:
            self.voice.set_preview_busy(self._previewing, False)
            self._previewing = ""
        self.voice.clear_preview_busy()

    def _on_preview_timeout(self) -> None:
        voice_id = self._previewing or "this voice"
        self._finish_preview()
        self.show_error(
            OperationError(
                ErrorCode.TTS_TIMEOUT,
                f"“{voice_id}” is taking too long to preview.",
                reason=(
                    "The voice model did not respond within "
                    f"{PREVIEW_TIMEOUT_MS // 1000} seconds. The first use of a "
                    "voice downloads it, which can be slow on a poor connection."
                ),
                recommended_action="Try again, or pick a voice that is already downloaded.",
                operation="voice_preview",
            )
        )

    def _on_preview_ready(self, audio, sample_rate: int) -> None:
        voice_id = self._previewing
        self._finish_preview()
        import tempfile

        from app.audio.assemble import write_wav

        try:
            directory = Path(tempfile.gettempdir()) / "pediaid-voice-studio"
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"preview-{voice_id}.wav"
            write_wav(path, audio, sample_rate)
        except Exception as exc:
            self.show_error(
                capture(
                    exc,
                    ErrorCode.AUDIO_PROCESSING_FAILED,
                    user_message="The voice preview could not be played.",
                    recommended_action="Try again, or pick a different voice.",
                    operation="voice_preview",
                )
            )
            return

        from PySide6.QtCore import QUrl

        self.review.play_file(path)
        self._toast_message(f"Previewing {voice_id or 'voice'}", "info")

    def _on_preview_failed(self, error: OperationError) -> None:
        self._finish_preview()
        self.show_error(error)

    def set_appearance(self, appearance: Appearance) -> None:
        application = QApplication.instance()
        if application is not None:
            apply_theme(application, appearance)
            self._toast_message(f"{appearance.value.title()} appearance", "info")

    # -- errors ----------------------------------------------------------

    def show_error(self, error: OperationError) -> None:
        """The one place a failure becomes visible."""
        action = show_error(error, self)
        if action == "retry":
            if error.operation in ("tts_generation", "generate", "audio_fit"):
                self.go("generate")
                self.generate.retry_failed() if error.segment else self.generate.start()
            elif error.operation.startswith("export"):
                self.go("export")
        elif action == "change_voice":
            self.go("voice")
        elif action == "open_settings":
            self.go("settings")
        elif action == "choose_folder":
            self.go("export")
        elif action == "choose_file":
            self.choose_subtitles()

    # -- lifecycle -------------------------------------------------------

    def _confirm_discard(self) -> bool:
        if not self.state.document.is_dirty:
            return True
        answer = QMessageBox.question(
            self,
            "Unsaved changes",
            "This project has unsaved changes.\n\nSave before continuing?",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Cancel:
            return False
        if answer == QMessageBox.StandardButton.Save:
            self.save_project()
            return not self.state.document.is_dirty
        return True

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.state.state is OperationState.GENERATING:
            answer = QMessageBox.question(
                self,
                "Generation in progress",
                "The narration is still being generated.\n\n"
                "Closing now will stop it. Sections already generated are cached, "
                "so restarting later will be faster.\n\nStop and close?",
                QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Close,
            )
            if answer == QMessageBox.StandardButton.Cancel:
                event.ignore()
                return
            self.generate.cancel()

        if not self._confirm_discard():
            event.ignore()
            return

        self.state.settings.save()
        store.clear_recovery()
        self.review.stop()
        # Stop worker threads before the interpreter tears down, otherwise Qt
        # aborts with "Destroyed while thread is still running".
        wait_for_threads()
        event.accept()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._toast.isVisible():
            self._toast._reposition()
