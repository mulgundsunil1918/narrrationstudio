"""Terminology and pronunciation editor.

Two dictionaries, both entirely user-owned:

* **Terminology** rewrites the *captions* — what the viewer reads.
* **Pronunciation** rewrites only what the *engine* hears — captions are untouched.

Nothing ships preloaded, because the right terms depend on the subject of the
script. This is where they get added, and everything that will ever run is
visible here first.
"""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLineEdit,
    QScrollArea,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from app.config import rules_path, support_dir
from app.core.status import ErrorCode, OperationError
from app.srt import cleanup
from app.srt.cleanup import ReplacementRule
from app.tts import pronunciation
from app.tts.pronunciation import PronunciationEntry, spell_out
from app.ui.theme import palette
from app.ui.widgets.common import (
    Card,
    GhostButton,
    PrimaryButton,
    SecondaryButton,
    caption,
    clear_layout,
    label,
    muted,
    section_label,
    title,
)


def pronunciation_path() -> Path:
    return support_dir() / "pronunciation.json"


class _Row(QWidget):
    """Base for one editable dictionary entry."""

    removed = Signal(object)
    changed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.line = QHBoxLayout(self)
        self.line.setContentsMargins(0, 0, 0, 0)
        self.line.setSpacing(8)

    def _delete_button(self) -> GhostButton:
        button = GhostButton("✕")
        button.setFixedWidth(34)
        button.setToolTip("Remove this entry")
        button.clicked.connect(lambda: self.removed.emit(self))
        return button

    def _arrow(self):
        arrow = label("→", "Muted")
        arrow.setFixedWidth(16)
        arrow.setAlignment(Qt.AlignmentFlag.AlignCenter)
        return arrow


class TerminologyRow(_Row):
    """find → replace, with matching options."""

    def __init__(self, rule: ReplacementRule, parent=None) -> None:
        super().__init__(parent)

        self.enabled = QCheckBox()
        self.enabled.setChecked(rule.enabled)
        self.enabled.setToolTip("Include this rule when enhancing")
        self.enabled.stateChanged.connect(lambda _s: self.changed.emit())
        self.line.addWidget(self.enabled)

        self.find = QLineEdit(rule.pattern)
        self.find.setPlaceholderText("What the transcript says")
        self.find.textChanged.connect(lambda _t: self.changed.emit())
        self.line.addWidget(self.find, 3)

        self.line.addWidget(self._arrow())

        self.replace = QLineEdit(rule.replacement)
        self.replace.setPlaceholderText("What it should say")
        self.replace.textChanged.connect(lambda _t: self.changed.emit())
        self.line.addWidget(self.replace, 3)

        self.whole_word = QCheckBox("Whole word")
        self.whole_word.setChecked(rule.whole_word)
        self.whole_word.setToolTip("Do not match inside a longer word")
        self.whole_word.stateChanged.connect(lambda _s: self.changed.emit())
        self.line.addWidget(self.whole_word)

        self.case = QCheckBox("Aa")
        self.case.setChecked(rule.case_sensitive)
        self.case.setToolTip("Match capitalisation exactly")
        self.case.stateChanged.connect(lambda _s: self.changed.emit())
        self.line.addWidget(self.case)

        self.regex = QCheckBox(".*")
        self.regex.setChecked(rule.is_regex)
        self.regex.setToolTip("Treat the search text as a regular expression")
        self.regex.stateChanged.connect(lambda _s: self.changed.emit())
        self.line.addWidget(self.regex)

        self.line.addWidget(self._delete_button())

    def to_rule(self) -> ReplacementRule:
        return ReplacementRule(
            pattern=self.find.text().strip(),
            replacement=self.replace.text(),
            whole_word=self.whole_word.isChecked(),
            case_sensitive=self.case.isChecked(),
            is_regex=self.regex.isChecked(),
            enabled=self.enabled.isChecked(),
        )


class PronunciationRow(_Row):
    """term → how the engine should say it."""

    def __init__(self, entry: PronunciationEntry, parent=None) -> None:
        super().__init__(parent)

        self.enabled = QCheckBox()
        self.enabled.setChecked(entry.enabled)
        self.enabled.stateChanged.connect(lambda _s: self.changed.emit())
        self.line.addWidget(self.enabled)

        self.term = QLineEdit(entry.term)
        self.term.setPlaceholderText("Word or acronym in the caption")
        self.term.textChanged.connect(lambda _t: self.changed.emit())
        self.line.addWidget(self.term, 3)

        self.line.addWidget(self._arrow())

        self.spoken = QLineEdit(entry.spoken)
        self.spoken.setPlaceholderText("How it should be pronounced")
        self.spoken.textChanged.connect(lambda _t: self.changed.emit())
        self.line.addWidget(self.spoken, 3)

        letters = GhostButton("Spell out")
        letters.setToolTip("Read the term one letter at a time, e.g. B B C")
        letters.clicked.connect(self._spell_out)
        self.line.addWidget(letters)

        self.line.addWidget(self._delete_button())

    def _spell_out(self) -> None:
        term = self.term.text().strip()
        if term:
            self.spoken.setText(spell_out(term))

    def to_entry(self) -> PronunciationEntry:
        return PronunciationEntry(
            term=self.term.text().strip(),
            spoken=self.spoken.text().strip(),
            enabled=self.enabled.isChecked(),
        )


class DictionaryDialog(QDialog):
    """Manage both dictionaries. Saves only when the user accepts."""

    saved = Signal()

    def __init__(self, parent: QWidget | None = None, focus: str = "terminology") -> None:
        super().__init__(parent)
        self.setWindowTitle("Terminology & Pronunciation")
        self.setModal(True)
        self.setMinimumSize(880, 620)

        self._term_rows: list[TerminologyRow] = []
        self._pron_rows: list[PronunciationRow] = []
        self._error: OperationError | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 22, 24, 20)
        outer.setSpacing(14)

        outer.addWidget(title("Terminology & Pronunciation"))
        outer.addWidget(
            muted(
                "These lists are yours. Nothing is applied that is not written "
                "here, so the app never rewrites a word you did not ask it to.",
                wrap=True,
            )
        )

        self._tabs = QTabWidget()
        self._tabs.addTab(self._build_terminology(), "Terminology")
        self._tabs.addTab(self._build_pronunciation(), "Pronunciation")
        self._tabs.setCurrentIndex(0 if focus == "terminology" else 1)
        outer.addWidget(self._tabs, 1)

        outer.addLayout(self._build_actions())
        self.load()

    # -- construction ----------------------------------------------------

    def _build_terminology(self) -> QWidget:
        page = QWidget()
        column = QVBoxLayout(page)
        column.setContentsMargins(0, 14, 0, 0)
        column.setSpacing(11)

        column.addWidget(
            muted(
                "Fixes what the viewer reads. Use it for names the transcriber "
                "got wrong — a product name, a person, a piece of jargon.",
                wrap=True,
            )
        )
        column.addWidget(
            caption(
                "Example:  find “Acmee”  →  replace with “Acme”",
                wrap=True,
            )
        )

        self._term_scroll, self._term_holder = self._scroller()
        column.addWidget(self._term_scroll, 1)

        row = QHBoxLayout()
        row.setSpacing(8)
        add = SecondaryButton("＋  Add Rule")
        add.clicked.connect(lambda: self._add_term())
        row.addWidget(add)
        row.addStretch(1)
        self._term_count = caption("")
        row.addWidget(self._term_count)
        column.addLayout(row)
        return page

    def _build_pronunciation(self) -> QWidget:
        page = QWidget()
        column = QVBoxLayout(page)
        column.setContentsMargins(0, 14, 0, 0)
        column.setSpacing(11)

        column.addWidget(
            muted(
                "Changes only what the voice says, never the caption. Use it for "
                "acronyms and names the engine mispronounces.",
                wrap=True,
            )
        )
        column.addWidget(
            caption(
                "Example:  “NASA” → “NASA”  ·  “API” → “A P I”  ·  “Acme” → “Ack-me”",
                wrap=True,
            )
        )

        self._pron_scroll, self._pron_holder = self._scroller()
        column.addWidget(self._pron_scroll, 1)

        row = QHBoxLayout()
        row.setSpacing(8)
        add = SecondaryButton("＋  Add Pronunciation")
        add.clicked.connect(lambda: self._add_pron())
        row.addWidget(add)
        row.addStretch(1)
        self._pron_count = caption("")
        row.addWidget(self._pron_count)
        column.addLayout(row)
        return page

    def _scroller(self) -> tuple[QScrollArea, QVBoxLayout]:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        holder = QWidget()
        column = QVBoxLayout(holder)
        column.setContentsMargins(0, 0, 8, 0)
        column.setSpacing(7)
        column.setAlignment(Qt.AlignmentFlag.AlignTop)
        scroll.setWidget(holder)
        return scroll, column

    def _build_actions(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(9)

        import_button = GhostButton("Import…")
        import_button.setToolTip("Load a dictionary from a JSON file")
        import_button.clicked.connect(self._import)
        row.addWidget(import_button)

        export_button = GhostButton("Export…")
        export_button.setToolTip("Save this dictionary to a JSON file you can reuse")
        export_button.clicked.connect(self._export)
        row.addWidget(export_button)

        row.addStretch(1)

        cancel = SecondaryButton("Cancel")
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)

        save = PrimaryButton("Save")
        save.clicked.connect(self._save)
        row.addWidget(save)
        return row

    # -- rows ------------------------------------------------------------

    def _add_term(self, rule: ReplacementRule | None = None) -> TerminologyRow:
        row = TerminologyRow(rule or ReplacementRule("", ""))
        row.removed.connect(self._remove_term)
        row.changed.connect(self._refresh_counts)
        self._term_holder.addWidget(row)
        self._term_rows.append(row)
        self._refresh_counts()
        if rule is None:
            row.find.setFocus()
        return row

    def _remove_term(self, row: TerminologyRow) -> None:
        if row in self._term_rows:
            self._term_rows.remove(row)
        row.setParent(None)
        row.deleteLater()
        self._refresh_counts()

    def _add_pron(self, entry: PronunciationEntry | None = None) -> PronunciationRow:
        row = PronunciationRow(entry or PronunciationEntry("", ""))
        row.removed.connect(self._remove_pron)
        row.changed.connect(self._refresh_counts)
        self._pron_holder.addWidget(row)
        self._pron_rows.append(row)
        self._refresh_counts()
        if entry is None:
            row.term.setFocus()
        return row

    def _remove_pron(self, row: PronunciationRow) -> None:
        if row in self._pron_rows:
            self._pron_rows.remove(row)
        row.setParent(None)
        row.deleteLater()
        self._refresh_counts()

    def _refresh_counts(self) -> None:
        terms = len([r for r in self._term_rows if r.find.text().strip()])
        prons = len([r for r in self._pron_rows if r.term.text().strip()])
        self._term_count.setText(f"{terms} rule{'s' if terms != 1 else ''}")
        self._pron_count.setText(f"{prons} entr{'ies' if prons != 1 else 'y'}")

    # -- persistence -----------------------------------------------------

    def load(self) -> None:
        clear_layout(self._term_holder)
        clear_layout(self._pron_holder)
        self._term_rows.clear()
        self._pron_rows.clear()

        try:
            rules = cleanup.load_rules(rules_path())
        except Exception:
            rules = []
        try:
            entries = pronunciation.load(pronunciation_path())
        except Exception:
            entries = []

        for rule in rules:
            self._add_term(rule)
        for entry in entries:
            self._add_pron(entry)

        # An empty list with no row is a dead end; give the user something to type into.
        if not self._term_rows:
            self._add_term()
        if not self._pron_rows:
            self._add_pron()
        self._refresh_counts()

    def terminology_rules(self) -> list[ReplacementRule]:
        return [r.to_rule() for r in self._term_rows if r.to_rule().pattern]

    def pronunciation_entries(self) -> list[PronunciationEntry]:
        return [
            r.to_entry() for r in self._pron_rows if r.to_entry().term and r.to_entry().spoken
        ]

    def _save(self) -> None:
        rules = self.terminology_rules()
        # Reject a broken regex here rather than at generation time.
        for rule in rules:
            if rule.is_regex:
                try:
                    rule.compile()
                except Exception as exc:
                    self._error = OperationError(
                        ErrorCode.UNKNOWN_ERROR,
                        f"The rule “{rule.pattern}” is not a valid pattern.",
                        reason=str(exc),
                        recommended_action=(
                            "Fix the expression, or turn off the “.*” option for "
                            "that rule."
                        ),
                        operation="dictionary_save",
                    )
                    self.done(QDialog.DialogCode.Rejected)
                    return
        try:
            cleanup.save_rules(rules_path(), rules)
            pronunciation.save(pronunciation_path(), self.pronunciation_entries())
        except Exception as exc:
            self._error = OperationError(
                ErrorCode.FILE_PERMISSION_DENIED,
                "Your dictionary could not be saved.",
                reason=str(exc),
                recommended_action="Check that your Application Support folder is writable.",
                operation="dictionary_save",
            )
            self.done(QDialog.DialogCode.Rejected)
            return

        self.saved.emit()
        self.accept()

    @property
    def error(self) -> OperationError | None:
        return self._error

    # -- import / export -------------------------------------------------

    def _import(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Import dictionary", str(Path.home()), "JSON files (*.json)"
        )
        if not path:
            return
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception as exc:
            self._error = OperationError(
                ErrorCode.FILE_NOT_FOUND,
                f"“{Path(path).name}” could not be read.",
                reason=str(exc),
                recommended_action="Choose a JSON file exported from this app.",
                operation="dictionary_import",
            )
            self.done(QDialog.DialogCode.Rejected)
            return

        added = 0
        for item in payload.get("rules", []) if isinstance(payload, dict) else []:
            if isinstance(item, dict) and item.get("pattern"):
                known = {f.name for f in ReplacementRule.__dataclass_fields__.values()}
                self._add_term(
                    ReplacementRule(**{k: v for k, v in item.items() if k in known})
                )
                added += 1
        for item in payload.get("entries", []) if isinstance(payload, dict) else []:
            if isinstance(item, dict) and item.get("term"):
                known = {f.name for f in PronunciationEntry.__dataclass_fields__.values()}
                self._add_pron(
                    PronunciationEntry(**{k: v for k, v in item.items() if k in known})
                )
                added += 1
        self._refresh_counts()

    def _export(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export dictionary",
            str(Path.home() / "Desktop" / "dictionary.json"),
            "JSON files (*.json)",
        )
        if not path:
            return
        from dataclasses import asdict

        payload = {
            "version": 1,
            "rules": [asdict(r) for r in self.terminology_rules()],
            "entries": [asdict(e) for e in self.pronunciation_entries()],
        }
        try:
            Path(path).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception as exc:
            self._error = OperationError(
                ErrorCode.FILE_PERMISSION_DENIED,
                "The dictionary could not be exported.",
                reason=str(exc),
                recommended_action="Choose a folder inside your home directory.",
                operation="dictionary_export",
            )
            self.done(QDialog.DialogCode.Rejected)
