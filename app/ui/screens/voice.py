"""Voice Studio: pick a voice, hear it, tune the basics.

The voice list is built from whatever engines report, never hard-coded here.
Style tags describe how a voice is useful in this app; they are not claims that
the model has an emotion control, and anything the engine cannot actually do is
disabled rather than faked.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QHBoxLayout,
    QRadioButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.config import VOICE_PREVIEW_TEXT
from app.tts.base import Voice
from app.tts.registry import all_voices, engine as get_engine, engine_ids
from app.ui.state import AppState
from app.ui.theme import palette
from app.ui.widgets.common import (
    Card,
    Divider,
    GhostButton,
    LabeledSlider,
    Pill,
    SecondaryButton,
    Segmented,
    caption,
    clear_layout,
    heading,
    label,
    muted,
    section_label,
    title,
)

#: Presets map to concrete engine + fitting settings. Only settings the engine
#: genuinely supports are touched.
VOICE_PRESETS = {
    "Natural": {"speed": 1.0},
    "Professional": {"speed": 0.97},
    "Medical": {"speed": 0.94},
    "Warm": {"speed": 0.96},
    "Energetic": {"speed": 1.06},
    "Advertisement": {"speed": 1.04},
    "Podcast": {"speed": 0.98},
    "Calm": {"speed": 0.92},
}

CATEGORY_ORDER = (
    "Favourites", "Recently used", "Female", "Male",
    "Warm", "Bright", "Deep", "Calm", "Energetic",
    "Professional", "Narrator", "Medical / Educational",
)


class VoiceCard(Card):
    """One selectable voice."""

    selected = Signal(str)
    preview = Signal(str)
    favourited = Signal(str)

    def __init__(self, voice: Voice, state: AppState, parent=None) -> None:
        super().__init__(parent=parent)
        self._voice = voice
        self.voice_id = voice.identifier
        self._state = state
        self.body.setContentsMargins(16, 13, 16, 14)
        self.body.setSpacing(8)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        top = QHBoxLayout()
        top.setSpacing(9)

        column = QVBoxLayout()
        column.setSpacing(2)
        column.addWidget(heading(voice.name))
        column.addWidget(muted(f"{voice.gender} · {voice.language}"))
        top.addLayout(column, 1)

        self._star = GhostButton("★" if state.is_favourite(voice.identifier) else "☆")
        self._star.setFixedWidth(34)
        self._star.setToolTip("Add to favourites")
        self._star.clicked.connect(lambda: self.favourited.emit(voice.identifier))
        top.addWidget(self._star)
        self.add_layout(top)

        if voice.tags:
            tags = QHBoxLayout()
            tags.setSpacing(5)
            for tag in voice.tags[:3]:
                chip = Pill(tag, "neutral")
                tags.addWidget(chip)
            tags.addStretch(1)
            self.add_layout(tags)

        if voice.notes:
            self.add(caption(voice.notes, wrap=True))

        actions = QHBoxLayout()
        actions.setSpacing(7)
        self._preview_button = SecondaryButton("▶  Preview")
        self._preview_button.clicked.connect(lambda: self.preview.emit(voice.identifier))
        actions.addWidget(self._preview_button)
        self._use = SecondaryButton("Use Voice")
        self._use.clicked.connect(lambda: self.selected.emit(voice.identifier))
        actions.addWidget(self._use)
        actions.addStretch(1)
        self.add_layout(actions)

        self.refresh()

    def mousePressEvent(self, event) -> None:
        self.selected.emit(self._voice.identifier)
        super().mousePressEvent(event)

    def refresh(self) -> None:
        current = palette()
        active = self._state.voice.voice == self._voice.identifier
        self.setStyleSheet(
            f"#Card {{ background-color: {current.accent_soft if active else current.surface};"
            f" border: {'2px' if active else '1px'} solid"
            f" {current.accent if active else current.border}; border-radius: 12px; }}"
        )
        self._use.setText("✓  In use" if active else "Use Voice")
        self._use.setEnabled(not active)
        self._star.setText("★" if self._state.is_favourite(self._voice.identifier) else "☆")

    def set_previewing(self, busy: bool) -> None:
        self._preview_button.setEnabled(not busy)
        self._preview_button.setText("Loading…" if busy else "▶  Preview")


#: Which Kokoro language codes each filter covers. Indian first after "any",
#: because it is the one people most often come here looking for and hunting
#: for it down a list of forty voices is the whole problem.
LANGUAGE_CODES: dict[str, tuple[str, ...]] = {
    "Indian": ("h",),
    "US English": ("a",),
    "British": ("b",),
    "Other": ("e", "f", "i", "p"),
}

LANGUAGE_FILTERS: tuple[tuple[str, str], ...] = (
    ("Any language", "Any language"),
    ("Indian", "Indian"),
    ("US English", "US English"),
    ("British", "British"),
    ("Other", "Other"),
)


class VoiceScreen(QWidget):
    """The voice library and the basic voice controls."""

    preview_requested = Signal(str)

    def __init__(self, state: AppState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._state = state
        self._cards: list[VoiceCard] = []
        self._filter = "All"
        self._language = "Any language"

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build_header())

        split = QHBoxLayout()
        split.setContentsMargins(0, 0, 0, 0)
        split.setSpacing(0)
        split.addWidget(self._build_library(), 1)
        split.addWidget(self._build_controls())
        outer.addLayout(split, 1)

        state.voice_changed.connect(self._refresh_cards)
        self.reload()

    # -- construction ----------------------------------------------------

    def _build_header(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("TopBar")
        row = QHBoxLayout(bar)
        row.setContentsMargins(28, 16, 28, 16)
        row.setSpacing(12)

        column = QVBoxLayout()
        column.setSpacing(2)
        column.addWidget(title("Voice"))
        self._subtitle = muted("Choose the voice for your narration.")
        column.addWidget(self._subtitle)
        row.addLayout(column)
        row.addStretch(1)

        self._locality = Pill("● Local processing", "success")
        row.addWidget(self._locality)
        return bar

    def _build_library(self) -> QWidget:
        wrapper = QWidget()
        wrapper.setObjectName("Workspace")
        column = QVBoxLayout(wrapper)
        column.setContentsMargins(28, 18, 20, 20)
        column.setSpacing(14)

        # Two rows, because language and voice quality are separate questions.
        # Cramming them into one strip made "Indian" sit beside "Female" as
        # though picking one ruled out the other.
        self._languages = Segmented(
            [(key, label) for key, label in LANGUAGE_FILTERS], initial="Any language"
        )
        self._languages.changed.connect(self._on_language)
        column.addWidget(self._languages)

        self._categories = Segmented(
            [(name, name) for name in ("All", "Favourites", "Female", "Male", "Narrator")],
            initial="All",
        )
        self._categories.changed.connect(self._on_filter)
        column.addWidget(self._categories)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        holder = QWidget()
        self._grid = QVBoxLayout(holder)
        self._grid.setContentsMargins(0, 0, 10, 0)
        self._grid.setSpacing(10)
        self._grid.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._scroll.setWidget(holder)
        column.addWidget(self._scroll, 1)
        return wrapper

    def _build_controls(self) -> QWidget:
        panel = QWidget()
        panel.setObjectName("RightPanel")
        panel.setFixedWidth(300)
        column = QVBoxLayout(panel)
        column.setContentsMargins(20, 20, 20, 20)
        column.setSpacing(18)
        column.setAlignment(Qt.AlignmentFlag.AlignTop)

        column.addWidget(section_label("Engine"))
        self._engine_combo = QComboBox()
        for identifier in engine_ids():
            backend = get_engine(identifier)
            available, _ = backend.is_available()
            suffix = "" if available else "  (not installed)"
            self._engine_combo.addItem(f"{backend.display_name}{suffix}", identifier)
        column.addWidget(self._engine_combo)

        column.addWidget(Divider())
        column.addWidget(section_label("Style preset"))
        self._preset_combo = QComboBox()
        self._preset_combo.addItems(list(VOICE_PRESETS))
        self._preset_combo.setCurrentText(self._state.voice.preset)
        self._preset_combo.currentTextChanged.connect(self._on_preset)
        column.addWidget(self._preset_combo)
        column.addWidget(
            caption(
                "A preset only adjusts the speaking rate. Tone shaping lives in "
                "Voice Enhancement after generation.",
                wrap=True,
            )
        )

        column.addWidget(Divider())
        column.addWidget(section_label("Voice controls"))

        self._speed = LabeledSlider("Speed", 70, 130, 100, "%")
        self._speed.valueChanged.connect(self._on_speed)
        column.addWidget(self._speed)

        self._volume = LabeledSlider("Volume", 0, 100, 100, "%")
        self._volume.valueChanged.connect(
            lambda v: setattr(self._state.voice, "volume", v / 100)
        )
        column.addWidget(self._volume)

        # Kokoro exposes no pitch control; showing an inert slider would be a lie.
        self._pitch = LabeledSlider("Pitch", -12, 12, 0, " st")
        self._pitch.setEnabled(False)
        column.addWidget(self._pitch)
        self._pitch_note = caption("The selected engine does not support pitch shifting.", wrap=True)
        column.addWidget(self._pitch_note)

        column.addStretch(1)

        self._preview_all = SecondaryButton("▶  Preview This Voice")
        self._preview_all.clicked.connect(
            lambda: self.preview_requested.emit(self._state.voice.voice)
        )
        column.addWidget(self._preview_all)
        column.addWidget(caption(f"“{VOICE_PREVIEW_TEXT}”", wrap=True))
        return panel

    # -- data ------------------------------------------------------------

    def reload(self) -> None:
        clear_layout(self._grid)
        self._cards.clear()

        voices = all_voices()
        if not voices:
            from app.ui.widgets.common import EmptyState

            self._grid.addWidget(
                EmptyState(
                    "🎙",
                    "No voices available",
                    "The local speech engine is not installed yet. Run setup.sh, "
                    "then reopen this screen.",
                )
            )
            self._locality.set_status("Engine unavailable", "error")
            return

        for voice in voices:
            if not self._matches(voice):
                continue
            card = VoiceCard(voice, self._state)
            card.selected.connect(self._on_selected)
            card.preview.connect(self.preview_requested)
            card.favourited.connect(self._on_favourite)
            self._grid.addWidget(card)
            self._cards.append(card)

        if not self._cards:
            self._grid.addWidget(muted("No voices match this filter.", wrap=True))
        self._refresh_cards()

    def _matches(self, voice: Voice) -> bool:
        return self._matches_language(voice) and self._matches_category(voice)

    def _matches_language(self, voice: Voice) -> bool:
        if self._language == "Any language":
            return True
        return voice.lang_code in LANGUAGE_CODES.get(self._language, ())

    def _matches_category(self, voice: Voice) -> bool:
        if self._filter == "All":
            return True
        if self._filter == "Favourites":
            return self._state.is_favourite(voice.identifier)
        if self._filter in ("Female", "Male"):
            return voice.gender == self._filter
        return self._filter in voice.tags

    def _on_filter(self, key: str) -> None:
        self._filter = key
        self.reload()

    def _on_language(self, key: str) -> None:
        self._language = key
        self.reload()

    def _refresh_cards(self) -> None:
        for card in self._cards:
            card.refresh()
        voice_id = self._state.voice.voice
        self._subtitle.setText(f"Using “{voice_id}” for this project.")
        self._speed.blockSignals(True)
        self._speed.setValue(int(round(self._state.voice.speed * 100)))
        self._speed.blockSignals(False)

    # -- actions ---------------------------------------------------------

    def _on_selected(self, voice_id: str) -> None:
        found = next((v for v in all_voices() if v.identifier == voice_id), None)
        self._state.set_voice(voice_id, found.lang_code if found else "a")
        self._state.report(f"Voice set to {found.name if found else voice_id}", "success")

    def _on_favourite(self, voice_id: str) -> None:
        added = self._state.toggle_favourite(voice_id)
        self._state.report(
            f"{'Added to' if added else 'Removed from'} favourites", "info"
        )
        if self._filter == "Favourites":
            self.reload()

    def _on_preset(self, name: str) -> None:
        preset = VOICE_PRESETS.get(name, {})
        self._state.voice.preset = name
        if "speed" in preset:
            self._speed.setValue(int(round(preset["speed"] * 100)))

    def _on_speed(self, value: int) -> None:
        self._state.voice.speed = value / 100

    def set_preview_busy(self, voice_id: str, busy: bool) -> None:
        self._preview_all.setEnabled(not busy)
        self._preview_all.setText("Loading…" if busy else "▶  Preview This Voice")
        for card in self._cards:
            if card.voice_id == voice_id:
                card.set_previewing(busy)

    def clear_preview_busy(self) -> None:
        """Reset every card. A card left saying "Loading…" is a dead end, so
        this runs whenever a preview ends for any reason."""
        self._preview_all.setEnabled(True)
        self._preview_all.setText("▶  Preview This Voice")
        for card in self._cards:
            card.set_previewing(False)
