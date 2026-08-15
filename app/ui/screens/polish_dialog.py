"""Polish the script with an AI, and bring the result back safely.

A three-step round trip: save the script out, hand it to ChatGPT with a prompt,
load the answer back. The app does the two ends; the AI does the middle.

The third step is where the care goes. A returned file is never loaded as a
document — it is matched against the captions already open and only its wording
is taken, so an AI that renumbers, merges or retimes cannot pull the narration
out of step with the video. Whatever did not line up is said out loud rather
than quietly dropped.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.core.errors import StudioError
from app.core.status import ErrorCode, OperationError, capture
from app.srt import reconcile
from app.srt.reconcile import Reconciliation
from app.ui.state import AppState
from app.ui.theme import palette
from app.ui.widgets.common import (
    Card,
    Divider,
    GhostButton,
    Pill,
    PrimaryButton,
    SecondaryButton,
    caption,
    clear_layout,
    heading,
    label,
    muted,
    section_label,
    title,
)

logger = logging.getLogger(__name__)


class PolishDialog(QDialog):
    """Save the script out, copy the prompt, load the AI's answer back in."""

    def __init__(self, state: AppState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._state = state
        self._saved_to: Path | None = None
        self._result: Reconciliation | None = None
        self.error: OperationError | None = None

        self.setWindowTitle("Polish the script with AI")
        self.setModal(True)
        self.setMinimumSize(820, 720)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(26, 24, 26, 20)
        outer.setSpacing(14)

        outer.addWidget(title("Polish the script with AI"))
        outer.addWidget(
            muted(
                "Speech-to-text mis-hears names and puts full stops in the wrong "
                "places, and that is the biggest single cause of narration that "
                "sounds unnatural. Send the script to ChatGPT to be tidied, then "
                "bring it back here.",
                wrap=True,
            )
        )

        promise = Pill(
            "🔒  Only the wording comes back — your timings are kept whatever the AI returns",
            "info",
        )
        promise.setAlignment(Qt.AlignmentFlag.AlignLeft)
        outer.addWidget(promise, alignment=Qt.AlignmentFlag.AlignLeft)

        outer.addWidget(self._build_steps())
        outer.addWidget(Divider())
        outer.addWidget(self._build_results(), 1)
        outer.addLayout(self._build_actions())

        self._show_placeholder()

    # -- the three steps -------------------------------------------------

    def _build_steps(self) -> QWidget:
        card = Card(quiet=True)
        card.body.setSpacing(12)

        card.add_layout(
            self._step(
                "1",
                "Save your script",
                "Writes an .srt with your timings in it. Keep the .srt — it is "
                "the version that can come back safely.",
                [("Save Script…", self.save_script, True),
                 ("Save as plain text", self.save_text, False)],
            )
        )
        card.add_layout(
            self._step(
                "2",
                "Send it to ChatGPT with this prompt",
                "The prompt tells it to fix the wording and leave every timestamp "
                "alone. Upload the file you just saved in the same message.",
                [("Copy the Prompt", self.copy_prompt, True),
                 ("See the prompt…", self.show_prompt, False)],
            )
        )
        card.add_layout(
            self._step(
                "3",
                "Bring the answer back",
                "Download the file ChatGPT gives you, then open it here. You will "
                "see every change before anything is applied.",
                [("Open the Edited File…", self.load_edited, True)],
            )
        )
        return card

    def _step(self, number: str, name: str, detail: str, buttons) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(13)

        current = palette()
        badge = label(number)
        badge.setFixedSize(24, 24)
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge.setStyleSheet(
            f"color: {current.accent}; background-color: {current.accent_soft};"
            f" border-radius: 12px; font-size: 11px; font-weight: 700;"
        )
        row.addWidget(badge, alignment=Qt.AlignmentFlag.AlignTop)

        column = QVBoxLayout()
        column.setSpacing(2)
        column.addWidget(label(name, "Body"))
        column.addWidget(caption(detail, wrap=True))

        actions = QHBoxLayout()
        actions.setContentsMargins(0, 6, 0, 2)
        actions.setSpacing(8)
        for text, handler, primary in buttons:
            button = SecondaryButton(text) if primary else GhostButton(text)
            button.clicked.connect(handler)
            actions.addWidget(button)
        actions.addStretch(1)
        column.addLayout(actions)

        if number == "1":
            self._saved_note = caption("")
            column.addWidget(self._saved_note)

        row.addLayout(column, 1)
        return row

    # -- results ---------------------------------------------------------

    def _build_results(self) -> QWidget:
        wrapper = QWidget()
        column = QVBoxLayout(wrapper)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(10)

        header = QHBoxLayout()
        header.addWidget(heading("What came back"))
        header.addStretch(1)
        self._count = Pill("—", "neutral")
        header.addWidget(self._count)
        column.addLayout(header)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        holder = QWidget()
        self._list = QVBoxLayout(holder)
        self._list.setContentsMargins(0, 0, 8, 0)
        self._list.setSpacing(9)
        self._list.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._scroll.setWidget(holder)
        column.addWidget(self._scroll, 1)
        return wrapper

    def _build_actions(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(9)
        row.addStretch(1)

        self._close = SecondaryButton("Close")
        self._close.clicked.connect(self.reject)
        row.addWidget(self._close)

        self._apply = PrimaryButton("Apply the New Wording")
        self._apply.setEnabled(False)
        self._apply.clicked.connect(self.apply_changes)
        row.addWidget(self._apply)
        return row

    def _show_placeholder(self) -> None:
        clear_layout(self._list)
        self._count.set_status("Nothing loaded yet", "neutral")
        self._list.addWidget(
            muted(
                "Work through the three steps above. When you open the edited "
                "file, every change it wants to make is listed here for you to "
                "check before anything happens to your script.",
                wrap=True,
            )
        )

    # -- step 1 ----------------------------------------------------------

    def _suggested_name(self, suffix: str) -> str:
        stem = (self._state.project.name or "script").strip() or "script"
        return str(Path.home() / "Desktop" / f"{stem}{suffix}")

    def save_script(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Save the script", self._suggested_name(".srt"), "Subtitles (*.srt)"
        )
        if not path:
            return
        target = Path(path)
        if target.suffix.lower() != ".srt":
            target = target.with_suffix(".srt")
        try:
            from app.srt.writer import write_srt

            write_srt(target, self._state.segments)
        except OSError as exc:
            self._fail(exc, f"“{target.name}” could not be saved.")
            return
        self._saved_to = target
        self._saved_note.setText(f"Saved to {target}. Upload that file to ChatGPT.")
        self._state.report(f"Saved {target.name}", "success")

    def save_text(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Save the script as text", self._suggested_name(".txt"), "Text (*.txt)"
        )
        if not path:
            return
        target = Path(path)
        try:
            target.write_text(
                reconcile.to_plain_text(self._state.segments), encoding="utf-8"
            )
        except OSError as exc:
            self._fail(exc, f"“{target.name}” could not be saved.")
            return
        self._saved_to = target
        self._saved_note.setText(
            f"Saved to {target}. Plain text has no timings in it, so it can only "
            "come back if the number of lines stays exactly the same."
        )
        self._state.report(f"Saved {target.name}", "success")

    # -- step 2 ----------------------------------------------------------

    def copy_prompt(self) -> None:
        from app.resources.srt_prompt import build_prompt

        clipboard = QGuiApplication.clipboard()
        if clipboard is None:
            self._state.report("The clipboard is not available.", "error")
            return
        clipboard.setText(build_prompt())
        self._state.report(
            "Prompt copied. Paste it into ChatGPT with your file.", "success"
        )

    def show_prompt(self) -> None:
        from app.ui.screens.prompt_dialog import PromptDialog

        PromptDialog(self).exec()

    # -- step 3 ----------------------------------------------------------

    def load_edited(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open the edited script",
            str(Path.home() / "Downloads"),
            "Edited script (*.srt *.txt *.md);;All files (*)",
        )
        if not path:
            return
        self.load_file(Path(path))

    def load_file(self, path: Path) -> None:
        """Parse a returned file and show what it would change. Applies nothing."""
        try:
            if path.suffix.lower() in (".txt", ".md", ".markdown"):
                from app.srt.parser import read_text_file

                result = reconcile.from_plain_text(
                    self._state.segments, read_text_file(path)
                )
            else:
                from app.srt.parser import parse_srt, read_text_file

                parsed = parse_srt(read_text_file(path), path=path)
                result = reconcile.reconcile(self._state.segments, parsed.segments)
        except StudioError as exc:
            self._show_failure(
                getattr(exc, "message", str(exc)),
                getattr(exc, "suggestion", "") or "Choose the file ChatGPT gave you.",
            )
            return
        except Exception as exc:
            self._fail(exc, f"“{path.name}” could not be read.")
            return

        self._result = result
        self._render(result, path)

    def _render(self, result: Reconciliation, path: Path) -> None:
        clear_layout(self._list)

        if not result.is_usable:
            self._apply.setEnabled(False)
            self._count.set_status("Cannot be used", "error")
            self._list.addWidget(label(f"“{path.name}” does not fit this script.", "Heading"))
            for problem in result.problems:
                self._list.addWidget(muted(problem, wrap=True))
            self._list.addWidget(
                caption(
                    "Ask ChatGPT for the complete .srt with the original "
                    "timestamps, as one downloadable file.",
                    wrap=True,
                )
            )
            return

        for problem in result.problems:
            self._list.addWidget(self._note_card(problem))

        if not result.changes:
            self._apply.setEnabled(False)
            self._count.set_status("No wording changed", "success")
            self._list.addWidget(
                muted(
                    "That file matched your script, but the wording is identical "
                    "to what you already have. Nothing to apply.",
                    wrap=True,
                )
            )
            return

        self._apply.setEnabled(True)
        self._count.set_status(
            f"{result.changed} of {result.matched} subtitles reworded",
            "info" if result.is_clean else "warning",
        )
        for change in result.changes[:300]:
            self._list.addWidget(self._diff_card(change))
        if result.changed > 300:
            self._list.addWidget(
                caption(f"…and {result.changed - 300} more.", wrap=True)
            )

    def _note_card(self, text: str) -> QWidget:
        current = palette()
        card = Card()
        card.body.setContentsMargins(15, 12, 15, 12)
        message = label(text, "Body", wrap=True)
        message.setStyleSheet(f"color: {current.text};")
        card.add(message)
        card.setStyleSheet(
            f"Card {{ background-color: {current.warning_soft};"
            f" border: 1px solid {current.warning}; border-radius: 10px; }}"
        )
        return card

    def _diff_card(self, change) -> QWidget:
        current = palette()
        card = Card()
        card.body.setContentsMargins(16, 13, 16, 14)
        card.body.setSpacing(7)
        card.add(caption(f"Subtitle {change.index + 1}"))

        before = label(change.before, "Body", wrap=True)
        before.setStyleSheet(
            f"color: {current.text_dim}; background: {current.danger_soft};"
            f" border-radius: 6px; padding: 7px 9px;"
        )
        card.add(before)

        after = label(change.after, "Body", wrap=True)
        after.setStyleSheet(
            f"color: {current.text}; background: {current.success_soft};"
            f" border-radius: 6px; padding: 7px 9px;"
        )
        card.add(after)
        return card

    def _show_failure(self, message: str, suggestion: str) -> None:
        clear_layout(self._list)
        self._apply.setEnabled(False)
        self._count.set_status("Could not read that file", "error")
        self._list.addWidget(label(message, "Heading"))
        self._list.addWidget(muted(suggestion, wrap=True))

    # -- applying --------------------------------------------------------

    def apply_changes(self) -> None:
        if self._result is None or not self._result.changes:
            return
        changed = self._state.document.apply_text_map(
            self._result.as_text_map(), "Polish script"
        )
        self._state.report(
            f"Reworded {changed} subtitles. Every timing is unchanged.", "success"
        )
        self.accept()

    def _fail(self, exc: Exception, message: str) -> None:
        self.error = capture(
            exc,
            ErrorCode.SRT_INVALID,
            user_message=message,
            recommended_action="Choose a different file, or save somewhere else.",
            operation="polish_script",
        )
        self._state.error_raised.emit(self.error)
