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


#: What each prompt is for, shown as the switch and the explainer.
PURPOSES = {
    "write": (
        "Write the script",
        "For a silent video. ChatGPT watches it and writes the narration with "
        "timings that follow a strict speaking-pace budget — about 2.4 words a "
        "second, whole phrases per caption, gaps to breathe in. That budget is "
        "what stops the voice rushing in one place and dragging in the next.",
        "Your video never leaves ChatGPT’s chat — the app itself uploads nothing",
    ),
    "polish": (
        "Polish existing subtitles",
        "For an .srt you already have. Fixes mis-heard words, names and "
        "punctuation, tightens wording that cannot be spoken in its window, "
        "and reports pacing problems it is not allowed to fix.",
        "Your timestamps stay untouched — only the wording changes",
    ),
}


class PromptDialog(QDialog):
    """Shows the right prompt with the user's terms filled in, and copies it."""

    def __init__(
        self,
        parent: QWidget | None = None,
        brand: str = "",
        terms: str = "",
        purpose: str = "write",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("ChatGPT prompts")
        self.setModal(True)
        self.setMinimumSize(820, 700)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 22, 24, 20)
        outer.setSpacing(14)

        outer.addWidget(title("The prompt to hand to ChatGPT"))

        from app.ui.widgets.common import Segmented

        self._purpose = Segmented(
            [(key, name) for key, (name, _d, _p) in PURPOSES.items()],
            initial=purpose if purpose in PURPOSES else "write",
        )
        self._purpose.changed.connect(lambda _k: self._refresh())
        outer.addWidget(self._purpose)

        self._description = muted("", wrap=True)
        outer.addWidget(self._description)

        self._promise = Pill("", "info")
        self._promise.setAlignment(Qt.AlignmentFlag.AlignLeft)
        outer.addWidget(self._promise, alignment=Qt.AlignmentFlag.AlignLeft)

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
        from app.resources.srt_prompt import build_script_prompt

        key = self._purpose.current()
        _name, description, promise = PURPOSES[key]
        self._description.setText(description)
        self._promise.setText(promise)

        builder = build_script_prompt if key == "write" else build_prompt
        self._text.setPlainText(builder(self._brand.text(), self._terms.text()))

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
