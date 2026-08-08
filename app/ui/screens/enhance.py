"""Script enhancement: preview, compare, accept or reject.

Nothing is applied without the user seeing exactly what changes. The promise
that timestamps are untouched is stated on screen and is literally true — this
dialog only ever produces new *text*, and the document's timing API is not
called.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.srt import cleanup
from app.srt.cleanup import CleanupPreview, ReplacementRule
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

#: Each option maps to a set of replacement rules. Only rules the user can see
#: are ever applied -- there is no hidden grammar model rewriting clinical text.
OPTIONS = [
    ("brand", "Apply my terminology rules", True,
     "Applies the find-and-replace rules you have defined in Settings."),
    ("spacing", "Tidy spacing and stray characters", True,
     "Collapses double spaces and removes leading spaces."),
    ("punctuation", "Improve obvious punctuation", False,
     "Adds a missing space after a full stop or comma."),
]


def spacing_rules() -> list[ReplacementRule]:
    return [
        ReplacementRule(r"\s{2,}", " ", is_regex=True, note="collapse repeated spaces"),
        ReplacementRule(r"\s+([,.;:!?])", r"\1", is_regex=True, note="space before punctuation"),
    ]


def punctuation_rules() -> list[ReplacementRule]:
    return [
        ReplacementRule(
            r"([.,;:!?])([A-Za-z])", r"\1 \2", is_regex=True,
            note="missing space after punctuation",
        ),
    ]


class EnhanceDialog(QDialog):
    """Shows a before/after list and applies only when accepted."""

    def __init__(self, state: AppState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._state = state
        self._preview: CleanupPreview | None = None

        self.setWindowTitle("Enhance Script")
        self.setModal(True)
        self.setMinimumSize(760, 620)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(26, 24, 26, 22)
        outer.setSpacing(16)

        outer.addWidget(title("Enhance Script"))
        outer.addWidget(
            muted(
                "Improve spelling, punctuation and spoken flow while preserving "
                "your original meaning.",
                wrap=True,
            )
        )

        promise = Pill("🔒  Your subtitle timestamps will not be changed", "info")
        promise.setAlignment(Qt.AlignmentFlag.AlignLeft)
        outer.addWidget(promise, alignment=Qt.AlignmentFlag.AlignLeft)

        outer.addWidget(self._build_options())
        outer.addWidget(Divider())
        outer.addWidget(self._build_results(), 1)
        outer.addLayout(self._build_actions())

        self._recompute()

    # -- construction ----------------------------------------------------

    def _build_options(self) -> QWidget:
        card = Card(quiet=True)
        card.body.setSpacing(9)
        card.add(section_label("What to fix"))
        self._checks: dict[str, QCheckBox] = {}
        for key, text, default, hint in OPTIONS:
            box = QCheckBox(text)
            box.setChecked(default)
            box.setToolTip(hint)
            box.stateChanged.connect(lambda _s: self._recompute())
            card.add(box)
            self._checks[key] = box
        return card

    def _build_results(self) -> QWidget:
        wrapper = QWidget()
        column = QVBoxLayout(wrapper)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(10)

        header = QHBoxLayout()
        header.addWidget(heading("Proposed changes"))
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
        edit_rules = GhostButton("Edit Terminology…")
        edit_rules.setToolTip("Add or change the find-and-replace rules")
        edit_rules.clicked.connect(self.open_dictionary)
        row.addWidget(edit_rules)
        row.addStretch(1)

        reject = SecondaryButton("Reject")
        reject.clicked.connect(self.reject)
        row.addWidget(reject)

        self._accept = PrimaryButton("Accept Changes")
        self._accept.clicked.connect(self._apply)
        row.addWidget(self._accept)
        return row

    # -- behaviour -------------------------------------------------------

    def _active_rules(self) -> list[ReplacementRule]:
        rules: list[ReplacementRule] = []
        if self._checks["brand"].isChecked():
            rules += cleanup.load_rules(_rules_path())
        if self._checks["spacing"].isChecked():
            rules += spacing_rules()
        if self._checks["punctuation"].isChecked():
            rules += punctuation_rules()
        return rules

    def _recompute(self) -> None:
        clear_layout(self._list)

        rules = self._active_rules()
        texts = [segment.text for segment in self._state.segments]

        if self._checks["brand"].isChecked() and not cleanup.load_rules(_rules_path()):
            # An empty dictionary with a dead button is a dead end. Explain it
            # and give the user the one control that resolves it.
            self._preview = None
            self._count.set_status("No terminology rules yet", "warning")
            self._accept.setEnabled(False)
            self._list.addWidget(
                label("You haven't added any terminology rules yet.", "Heading")
            )
            self._list.addWidget(
                muted(
                    "Terminology rules fix words the transcriber got wrong — a "
                    "product name, a person, a piece of jargon. Nothing is "
                    "changed until you add one.",
                    wrap=True,
                )
            )
            button = PrimaryButton("Add Terminology Rules…")
            button.setFixedWidth(240)
            button.clicked.connect(self.open_dictionary)
            self._list.addWidget(button)
            return

        if not rules or not texts:
            self._preview = None
            self._count.set_status("No changes", "neutral")
            self._accept.setEnabled(False)
            self._list.addWidget(
                muted("Nothing to change with the current options.", wrap=True)
            )
            return

        try:
            self._preview = cleanup.preview(texts, rules)
        except Exception as exc:
            self._preview = None
            self._accept.setEnabled(False)
            self._count.set_status("Rule error", "error")
            self._list.addWidget(label(str(exc), "Body", wrap=True))
            return

        changed = self._preview.changed_previews
        if not changed:
            self._count.set_status("No changes needed", "success")
            self._accept.setEnabled(False)
            self._list.addWidget(
                muted("Your script already looks correct for these options.", wrap=True)
            )
            return

        self._count.set_status(
            f"{self._preview.replacement_count} changes in "
            f"{self._preview.segment_count} subtitles",
            "info",
        )
        self._accept.setEnabled(True)
        for item in changed[:200]:
            self._list.addWidget(self._diff_card(item))

    def _diff_card(self, item) -> QWidget:
        current = palette()
        card = Card()
        card.body.setContentsMargins(16, 13, 16, 14)
        card.body.setSpacing(7)

        card.add(caption(f"Subtitle {item.segment_index + 1}"))

        before = label(item.before, "Body", wrap=True)
        before.setStyleSheet(
            f"color: {current.text_dim}; background: {current.danger_soft};"
            f" border-radius: 6px; padding: 7px 9px;"
        )
        card.add(before)

        after = label(item.after, "Body", wrap=True)
        after.setStyleSheet(
            f"color: {current.text}; background: {current.success_soft};"
            f" border-radius: 6px; padding: 7px 9px;"
        )
        card.add(after)
        return card

    def _apply(self) -> None:
        if self._preview is None:
            self.reject()
            return
        mapping = self._preview.as_text_map()
        if not mapping:
            self.reject()
            return
        changed = self._state.document.apply_text_map(mapping, "Enhance script")
        self._state.report(f"Updated {changed} subtitles. Timings unchanged.", "success")
        self.accept()

    def open_dictionary(self) -> None:
        """Open the terminology editor, then re-run the preview with the result."""
        from app.ui.screens.dictionary import DictionaryDialog

        dialog = DictionaryDialog(self, focus="terminology")
        dialog.exec()
        if dialog.error is not None:
            self._state.error_raised.emit(dialog.error)
            return
        self._recompute()


def _rules_path():
    from app.config import rules_path

    return rules_path()
