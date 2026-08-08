"""Settings, including model management and diagnostics."""

from __future__ import annotations

import shutil
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLineEdit,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.cache.store import AudioCache
from app.config import audio_cache_dir, cache_dir
from app.logging_setup import log_path
from app.utils.platform import file_manager_name
from app.tts.registry import engine as get_engine, engine_ids
from app.ui.state import AppState
from app.ui.theme import Appearance
from app.ui.widgets.common import (
    Card,
    Divider,
    GhostButton,
    Pill,
    SecondaryButton,
    caption,
    clear_layout,
    heading,
    label,
    muted,
    section_label,
    title,
)


class SettingsScreen(QScrollArea):
    appearance_changed = Signal(object)

    def __init__(self, state: AppState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._state = state
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        holder = QWidget()
        holder.setObjectName("Workspace")
        column = QVBoxLayout(holder)
        column.setContentsMargins(28, 26, 28, 40)
        column.setSpacing(18)
        column.setAlignment(Qt.AlignmentFlag.AlignTop)

        column.addWidget(title("Settings"))
        column.addWidget(self._build_general())
        column.addWidget(self._build_models())
        column.addWidget(self._build_storage())
        column.addWidget(self._build_privacy())
        self.setWidget(holder)

    # -- sections --------------------------------------------------------

    def _build_general(self) -> QWidget:
        card = Card()
        card.add(section_label("General"))

        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(label("Appearance", "Body"))
        row.addStretch(1)
        self._appearance = QComboBox()
        self._appearance.addItem("Dark", Appearance.DARK)
        self._appearance.addItem("Light", Appearance.LIGHT)
        self._appearance.setFixedWidth(160)
        self._appearance.currentIndexChanged.connect(
            lambda i: self.appearance_changed.emit(self._appearance.itemData(i))
        )
        row.addWidget(self._appearance)
        card.add_layout(row)

        card.add(Divider())
        self._advanced = QCheckBox("Advanced mode")
        self._advanced.setChecked(self._state.advanced)
        self._advanced.toggled.connect(self._state.set_advanced)
        card.add(self._advanced)
        card.add(
            caption(
                "Shows exact timestamps, narration segmentation and technical "
                "detail throughout the app.",
                wrap=True,
            )
        )

        card.add(Divider())
        self._verbose = QCheckBox("Verbose logging (records subtitle text in the log)")
        self._verbose.setChecked(self._state.settings.verbose_logging)
        self._verbose.toggled.connect(self._on_verbose)
        card.add(self._verbose)
        return card

    def _build_models(self) -> QWidget:
        card = Card()
        header = QHBoxLayout()
        header.addWidget(section_label("Speech models"))
        header.addStretch(1)
        refresh = GhostButton("Refresh")
        refresh.clicked.connect(self._refresh_models)
        header.addWidget(refresh)
        card.add_layout(header)

        self._models_holder = QVBoxLayout()
        self._models_holder.setSpacing(10)
        card.add_layout(self._models_holder)
        self._refresh_models()
        return card

    def _refresh_models(self) -> None:
        clear_layout(self._models_holder)

        for identifier in engine_ids():
            backend = get_engine(identifier)
            available, why = backend.is_available()
            row = Card(quiet=True)
            row.body.setContentsMargins(15, 12, 15, 13)
            row.body.setSpacing(6)

            top = QHBoxLayout()
            top.setSpacing(10)
            top.addWidget(label(backend.display_name, "Heading"))
            top.addWidget(Pill(backend.locality.badge, "success" if available else "neutral"))
            top.addStretch(1)
            top.addWidget(
                Pill("Installed ✓" if available else "Not installed",
                     "success" if available else "error")
            )
            row.add_layout(top)

            if available:
                installed = getattr(backend, "installed_voice_files", lambda: set())()
                total = len(backend.voices())
                row.add(
                    muted(
                        f"{total} voices available · {len(installed)} downloaded"
                        if installed
                        else f"{total} voices available · downloaded on first use"
                    )
                )
                if installed:
                    row.add(caption("Downloaded: " + ", ".join(sorted(installed))))
            else:
                row.add(muted(why, wrap=True))
                row.add(
                    caption("Run ./setup.sh in the project folder to install it.", wrap=True)
                )
            self._models_holder.addWidget(row)

    def _build_storage(self) -> QWidget:
        card = Card()
        card.add(section_label("Storage"))

        cache = AudioCache(audio_cache_dir())
        size = cache.size_bytes()
        self._cache_label = muted(
            f"Generated audio cache: {size / 1e6:.1f} MB in {audio_cache_dir()}"
        )
        card.add(self._cache_label)
        card.add(
            caption(
                "Cached audio lets unchanged sections regenerate instantly. "
                "Clearing it is safe — it only costs time.",
                wrap=True,
            )
        )

        row = QHBoxLayout()
        row.addStretch(1)
        clear = SecondaryButton("Clear Cache")
        clear.clicked.connect(self._clear_cache)
        row.addWidget(clear)
        card.add_layout(row)

        card.add(Divider())
        card.add(label("Logs", "Body"))
        card.add(caption(str(log_path()), wrap=True))
        logs_row = QHBoxLayout()
        logs_row.addStretch(1)
        reveal = SecondaryButton(f"Show Log in {file_manager_name()}")
        reveal.clicked.connect(self._reveal_log)
        logs_row.addWidget(reveal)
        card.add_layout(logs_row)
        return card

    def _build_privacy(self) -> QWidget:
        card = Card(quiet=True)
        card.add(section_label("Privacy"))
        card.add(
            muted(
                "Everything happens on this Mac. No audio, subtitles or usage data "
                "leaves the machine. There is no account, no telemetry and no "
                "analytics. The only network access is the one-time download of a "
                "voice model, which you can see in Speech models above.",
                wrap=True,
            )
        )
        return card

    # -- actions ---------------------------------------------------------

    def _on_verbose(self, enabled: bool) -> None:
        from app.logging_setup import set_verbose

        self._state.settings.verbose_logging = enabled
        self._state.settings.save()
        set_verbose(enabled)
        self._state.report(
            "Verbose logging on — subtitle text will be written to the log."
            if enabled
            else "Verbose logging off.",
            "info",
        )

    def _clear_cache(self) -> None:
        try:
            removed = AudioCache(audio_cache_dir()).clear()
        except OSError as exc:
            self._state.report(f"The cache could not be cleared: {exc}", "error")
            return
        self._cache_label.setText(
            f"Generated audio cache: 0.0 MB in {audio_cache_dir()}"
        )
        self._state.report(f"Cleared {removed} cached files.", "success")

    def _reveal_log(self) -> None:
        from app.utils.platform import file_manager_name, reveal

        path = log_path()
        if not path.exists():
            self._state.report("No log file has been written yet.", "info")
            return
        ok, reason = reveal(path)
        if not ok:
            self._state.report(
                f"Could not open {file_manager_name()}: {reason}", "warning"
            )
