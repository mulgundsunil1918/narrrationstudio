"""The subtitle-preparation prompt, ready to copy into an AI chat.

Two fields fill the placeholders so the prompt carries the user's own product
name and vocabulary instead of someone else's.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLineEdit,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.resources.srt_prompt import build_prompt
from app.ui.theme import mono_font
from app.ui.widgets.common import (
    Card,
    GhostButton,
    Pill,
    PrimaryButton,
    SecondaryButton,
    caption,
    label,
    muted,
    section_label,
    title,
)


class PromptDialog(QDialog):
    """Shows the prompt with the user's terms filled in, and copies it."""

    def __init__(self, parent: QWidget | None = None, brand: str = "", terms: str = "") -> None:
        super().__init__(parent)
        self.setWindowTitle("Prepare your subtitles")
        self.setModal(True)
        self.setMinimumSize(820, 700)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 22, 24, 20)
        outer.setSpacing(14)

        outer.addWidget(title("Clean up your subtitles first"))
        outer.addWidget(
            muted(
                "Speech-to-text mis-hears names and puts full stops in the wrong "
                "places. That is the single biggest cause of narration that sounds "
                "unnatural. Paste this into ChatGPT, Claude or any AI along with "
                "your .srt file, then bring the result back here.",
                wrap=True,
            )
        )

        promise = Pill("Your timestamps stay untouched — only the wording changes", "info")
        promise.setAlignment(Qt.AlignmentFlag.AlignLeft)
        outer.addWidget(promise, alignment=Qt.AlignmentFlag.AlignLeft)

        outer.addWidget(self._build_fields(brand, terms))

        outer.addWidget(section_label("The prompt"))
        self._text = QPlainTextEdit()
        self._text.setReadOnly(True)
        self._text.setFont(mono_font(11))
        outer.addWidget(self._text, 1)

        outer.addLayout(self._build_actions())
        self._refresh()

    def _build_fields(self, brand: str, terms: str) -> QWidget:
        card = Card(quiet=True)
        card.body.setSpacing(10)
        card.add(section_label("Make it yours (optional)"))

        self._brand = QLineEdit(brand)
        self._brand.setPlaceholderText("Your brand or product name, e.g. Acme")
        self._brand.textChanged.connect(self._refresh)
        card.add(label("Name that must always be spelt correctly", "Muted"))
        card.add(self._brand)

        self._terms = QLineEdit(terms)
        self._terms.setPlaceholderText(
            "Specialist words the transcriber gets wrong, comma separated"
        )
        self._terms.textChanged.connect(self._refresh)
        card.add(label("Specialist terms to watch for", "Muted"))
        card.add(self._terms)
        card.add(
            caption(
                "Leave these blank and the prompt keeps visible placeholders for "
                "you to fill in later.",
                wrap=True,
            )
        )
        return card

    def _build_actions(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(9)
        self._status = caption("")
        row.addWidget(self._status)
        row.addStretch(1)

        save = GhostButton("Save to a file…")
        save.clicked.connect(self._save)
        row.addWidget(save)

        close = SecondaryButton("Close")
        close.clicked.connect(self.accept)
        row.addWidget(close)

        copy = PrimaryButton("Copy Prompt")
        copy.clicked.connect(self._copy)
        row.addWidget(copy)
        return row

    def _refresh(self) -> None:
        self._text.setPlainText(build_prompt(self._brand.text(), self._terms.text()))

    def _copy(self) -> None:
        clipboard = QGuiApplication.clipboard()
        if clipboard is None:
            self._status.setText("The clipboard is not available.")
            return
        clipboard.setText(self._text.toPlainText())
        self._status.setText("Copied. Paste it into your AI chat with your .srt file.")

    def _save(self) -> None:
        from pathlib import Path

        from PySide6.QtWidgets import QFileDialog

        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save the prompt",
            str(Path.home() / "Desktop" / "subtitle-prompt.txt"),
            "Text files (*.txt)",
        )
        if not path:
            return
        try:
            Path(path).write_text(self._text.toPlainText(), encoding="utf-8")
        except OSError as exc:
            self._status.setText(f"Could not save: {exc}")
            return
        self._status.setText(f"Saved to {Path(path).name}")
