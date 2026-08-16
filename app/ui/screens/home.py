"""Home: what this app does, and the one thing to do next."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.config import APP_NAME, PROJECT_SUFFIX
from app.projects import store
from app.ui.state import AppState
from app.ui.theme import palette
from app.ui.widgets.common import (
    Card,
    Divider,
    SecondaryButton,
    heading,
    section_label,
    GhostButton,
    SecondaryButton,
    caption,
    clear_layout,
    clock,
    display,
    heading,
    label,
    muted,
)
from app.ui.widgets.dropzone import DropZone


class HomeScreen(QScrollArea):
    """The landing surface: headline, drop target, recent projects."""

    open_file = Signal(Path, str)
    open_project = Signal(Path)
    rejected = Signal(Path, str)

    def __init__(self, state: AppState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._state = state
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        container = QWidget()
        container.setObjectName("Workspace")
        outer = QVBoxLayout(container)
        outer.setContentsMargins(56, 48, 56, 48)
        outer.setSpacing(26)
        outer.setAlignment(Qt.AlignmentFlag.AlignTop)

        outer.addLayout(self._build_hero())
        outer.addWidget(self._build_dropzone())
        outer.addWidget(self._build_how_it_works())
        outer.addLayout(self._build_recents())
        outer.addStretch(1)

        self.setWidget(container)
        state.project_saved.connect(lambda _p: self.refresh())

    # -- sections --------------------------------------------------------

    def _build_hero(self) -> QVBoxLayout:
        column = QVBoxLayout()
        column.setSpacing(9)

        title = display(APP_NAME)
        column.addWidget(title)

        subtitle = label(
            "Turn your script, subtitles or video into professional narration.",
            "Title",
            wrap=True,
        )
        subtitle.setStyleSheet("font-weight: 400;")
        column.addWidget(subtitle)

        badge = muted("● Everything runs on this Mac. Nothing is uploaded.")
        column.addWidget(badge)
        return column

    def _build_dropzone(self) -> QWidget:
        self._drop = DropZone(
            headline="Drop a video or subtitle file here",
            sub="SRT · TXT · MD · MP4 · MOV · WAV · MP3",
            button_text="New Project…",
        )
        self._drop.file_dropped.connect(self._on_dropped)
        self._drop.file_rejected.connect(self.rejected)
        return self._drop

    def _on_dropped(self, path: Path, kind: str) -> None:
        if kind == "project":
            self.open_project.emit(path)
        else:
            self.open_file.emit(path, kind)

    def _build_how_it_works(self) -> QWidget:
        """What this app is, and what has to happen before a file arrives here."""
        from app.resources.srt_prompt import WORKFLOW_STEPS

        card = Card()
        card.add(heading("How this works"))
        card.add(
            muted(
                "This app replaces the voice on a video you have already made. Drop "
                "the video in and it will listen to it and write the script for you, "
                "or bring your own script if the video is silent. Either way it "
                "speaks the words with a voice you pick and lines the audio up "
                "exactly with the original timings — so the narration still matches "
                "what is on screen.",
                wrap=True,
            )
        )
        card.add(Divider())
        card.add(section_label("Before you import a file"))

        for number, name, detail in WORKFLOW_STEPS:
            row = QHBoxLayout()
            row.setSpacing(13)

            badge = label(number)
            badge.setFixedSize(24, 24)
            badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
            current = palette()
            badge.setStyleSheet(
                f"color: {current.accent}; background-color: {current.accent_soft};"
                f" border-radius: 12px; font-size: 11px; font-weight: 700;"
            )
            row.addWidget(badge, alignment=Qt.AlignmentFlag.AlignTop)

            column = QVBoxLayout()
            column.setSpacing(2)
            column.addWidget(label(name, "Body"))
            column.addWidget(caption(detail, wrap=True))

            # The prompt belongs to the step that uses it, not in a footer where
            # it reads as unrelated.
            if number == "3":
                inline = QHBoxLayout()
                inline.setContentsMargins(0, 6, 0, 2)
                prompt_button = SecondaryButton("Get the ChatGPT prompts…")
                prompt_button.setToolTip(
                    "Ready-made prompts: one that writes the script with properly "
                    "paced timings, one that polishes an existing .srt"
                )
                prompt_button.clicked.connect(self.show_prompt)
                inline.addWidget(prompt_button)
                inline.addStretch(1)
                column.addLayout(inline)

            row.addLayout(column, 1)
            card.add_layout(row)
        return card

    def show_prompt(self) -> None:
        from app.ui.screens.prompt_dialog import PromptDialog

        PromptDialog(self).exec()

    def _build_recents(self) -> QVBoxLayout:
        column = QVBoxLayout()
        column.setSpacing(12)

        header = QHBoxLayout()
        header.addWidget(heading("Recent projects"))
        header.addStretch(1)
        self._open_button = GhostButton("Open Project…")
        self._open_button.clicked.connect(self._choose_project)
        header.addWidget(self._open_button)
        column.addLayout(header)

        self._recents_holder = QVBoxLayout()
        self._recents_holder.setSpacing(9)
        column.addLayout(self._recents_holder)

        self.refresh()
        return column

    # -- recents ---------------------------------------------------------

    def refresh(self) -> None:
        clear_layout(self._recents_holder)

        projects = store.recent_projects()
        if not projects:
            placeholder = Card(quiet=True)
            placeholder.body.setContentsMargins(20, 18, 20, 18)
            placeholder.add(label("No projects yet.", "Body"))
            placeholder.add(
                muted("Drop a subtitle file above to create your first narration.")
            )
            self._recents_holder.addWidget(placeholder)
            return

        for path in projects[:6]:
            self._recents_holder.addWidget(self._recent_row(path))

    def _recent_row(self, path: Path) -> QWidget:
        result = store.load(path)
        card = Card()
        card.body.setContentsMargins(18, 14, 18, 14)
        card.body.setSpacing(4)

        row = QHBoxLayout()
        row.setSpacing(14)

        column = QVBoxLayout()
        column.setSpacing(3)
        if result.success and result.value is not None:
            data = result.value
            column.addWidget(label(data.name or path.stem, "Heading"))
            details = " · ".join(
                filter(
                    None,
                    [
                        clock(data.duration_ms) if data.duration_ms else "",
                        f"{data.caption_count} subtitles" if data.caption_count else "",
                        data.voice,
                        f"edited {data.modified_at[:10]}" if data.modified_at else "",
                    ],
                )
            )
            column.addWidget(muted(details))
        else:
            column.addWidget(label(path.stem, "Heading"))
            column.addWidget(caption("This project could not be read."))
        row.addLayout(column, 1)

        open_button = SecondaryButton("Open")
        open_button.clicked.connect(lambda: self.open_project.emit(path))
        row.addWidget(open_button)

        card.add_layout(row)
        return card

    def _choose_project(self) -> None:
        from PySide6.QtWidgets import QFileDialog

        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open Project",
            str(Path.home()),
            f"{APP_NAME} projects (*{PROJECT_SUFFIX})",
        )
        if path:
            self.open_project.emit(Path(path))
