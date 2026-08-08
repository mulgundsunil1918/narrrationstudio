"""Review: hear the result and verify synchronisation.

The player highlights the caption currently being spoken, which is how the user
confirms the narration lines up without reading a single timestamp.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from PySide6.QtCore import QTimer, QUrl, Qt, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QHBoxLayout,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.core.status import OperationError, OperationState
from app.pipeline import GenerationOutcome
from app.ui.state import AppState
from app.ui.theme import palette
from app.ui.widgets.common import (
    Card,
    Divider,
    GhostButton,
    Metric,
    Pill,
    PrimaryButton,
    SecondaryButton,
    caption,
    clock,
    heading,
    label,
    muted,
    section_label,
    title,
)
from app.ui.widgets.waveform import TimelineView, WaveformView


class ReviewScreen(QWidget):
    """Playback, sync verification, and the hand-off to export."""

    export_requested = Signal()
    caption_active = Signal(int)

    def __init__(self, state: AppState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._state = state
        self._temp_path: Path | None = None

        self._player = QMediaPlayer(self)
        self._audio_out = QAudioOutput(self)
        self._player.setAudioOutput(self._audio_out)
        self._player.positionChanged.connect(self._on_position)
        self._player.playbackStateChanged.connect(self._on_playback_state)
        self._player.errorOccurred.connect(self._on_player_error)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build_header())

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        holder = QWidget()
        holder.setObjectName("Workspace")
        self._column = QVBoxLayout(holder)
        self._column.setContentsMargins(28, 22, 28, 32)
        self._column.setSpacing(18)
        self._column.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._column.addWidget(self._build_summary())
        self._column.addWidget(self._build_player())
        self._column.addWidget(self._build_sync())
        self._scroll.setWidget(holder)
        outer.addWidget(self._scroll, 1)

        state.generation_finished.connect(self.load_outcome)
        self.clear()

    # -- construction ----------------------------------------------------

    def _build_header(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("TopBar")
        row = QHBoxLayout(bar)
        row.setContentsMargins(28, 16, 28, 16)
        row.setSpacing(12)
        column = QVBoxLayout()
        column.setSpacing(2)
        column.addWidget(title("Preview"))
        column.addWidget(muted("Play the whole narration before exporting."))
        row.addLayout(column)
        row.addStretch(1)
        self._verdict = Pill("Not generated yet", "neutral")
        row.addWidget(self._verdict)
        return bar

    def _build_summary(self) -> QWidget:
        self._summary_card = Card()
        header = QHBoxLayout()
        self._headline = heading("Narration Ready")
        header.addWidget(self._headline)
        header.addStretch(1)
        self._export = PrimaryButton("Export…")
        self._export.clicked.connect(self.export_requested)
        header.addWidget(self._export)
        self._summary_card.add_layout(header)

        metrics = QHBoxLayout()
        metrics.setSpacing(30)
        self._m_duration = Metric("—", "DURATION")
        self._m_voice = Metric("—", "VOICE")
        self._m_segments = Metric("—", "SEGMENTS")
        self._m_sync = Metric("—", "SYNC")
        for metric in (self._m_duration, self._m_voice, self._m_segments, self._m_sync):
            metrics.addWidget(metric)
        metrics.addStretch(1)
        self._summary_card.add_layout(metrics)
        return self._summary_card

    def _build_player(self) -> QWidget:
        card = Card()
        card.add(section_label("Narration"))
        self.waveform = WaveformView()
        self.waveform.scrubbed.connect(self._seek)
        card.add(self.waveform)

        controls = QHBoxLayout()
        controls.setSpacing(9)
        self._previous = SecondaryButton("⏮")
        self._previous.setFixedWidth(48)
        self._previous.clicked.connect(self.previous_caption)
        self._play = PrimaryButton("▶  Play Full Narration")
        self._play.setFixedWidth(220)
        self._play.clicked.connect(self.toggle_play)
        self._next = SecondaryButton("⏭")
        self._next.setFixedWidth(48)
        self._next.clicked.connect(self.next_caption)
        controls.addWidget(self._previous)
        controls.addWidget(self._play)
        controls.addWidget(self._next)
        controls.addStretch(1)
        self._position = muted("0:00 / 0:00")
        controls.addWidget(self._position)
        card.add_layout(controls)
        return card

    def _build_sync(self) -> QWidget:
        card = Card()
        card.add(section_label("Synchronisation"))
        card.add(
            muted(
                "The caption being spoken right now is highlighted. Captions change "
                "on their own timings while the narration runs continuously.",
                wrap=True,
            )
        )
        self.timeline = TimelineView()
        self.timeline.caption_clicked.connect(self.jump_to_caption)
        card.add(self.timeline)

        card.add(Divider())
        self._now_playing = label("—", "Heading", wrap=True)
        card.add(self._now_playing)
        return card

    # -- content ---------------------------------------------------------

    def clear(self) -> None:
        self._summary_card.setVisible(False)
        self.waveform.set_audio(None, 0)
        self._verdict.set_status("Not generated yet", "neutral")
        self._now_playing.setText("Generate the narration to review it here.")

    def load_outcome(self, outcome: GenerationOutcome) -> None:
        self._summary_card.setVisible(True)
        self.waveform.set_audio(outcome.audio, outcome.sample_rate)

        self._m_duration.set_value(clock(outcome.duration_ms))
        self._m_voice.set_value(self._state.voice.voice)
        self._m_segments.set_value(str(len(outcome.plan)))

        if outcome.failures:
            self._m_sync.set_value("Partial")
            self._m_sync.set_tone("error")
            self._verdict.set_status(
                f"{len(outcome.failures)} segment(s) failed", "error"
            )
            self._headline.setText("Narration generated with problems")
        elif outcome.warnings:
            self._m_sync.set_value("✓")
            self._m_sync.set_tone("warning")
            self._verdict.set_status(f"{len(outcome.warnings)} notes", "warning")
            self._headline.setText("Narration Ready")
        else:
            self._m_sync.set_value("✓")
            self._m_sync.set_tone("success")
            self._verdict.set_status("Synchronized", "success")
            self._headline.setText("Narration Ready ✓")

        segments = self._state.segments
        window = self._state.window()
        self.timeline.set_data(
            [(s.start_ms, s.end_ms) for s in segments],
            [
                (window.start_ms(g), window.end_ms(g), g.forced_cut)
                for g in outcome.plan
            ],
            max(1, outcome.timeline_ms),
        )
        first = segments[0].text if segments else ""
        self._now_playing.setText(
            f"▶  {first}" if first else "Press Play to hear the narration."
        )
        self._prepare_playback(outcome)

    def _prepare_playback(self, outcome: GenerationOutcome) -> None:
        """Write the timeline to a temporary WAV so QMediaPlayer can stream it."""
        from app.audio.assemble import write_wav

        try:
            directory = Path(tempfile.gettempdir()) / "pediaid-voice-studio"
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / "preview.wav"
            write_wav(path, outcome.audio, outcome.sample_rate)
        except Exception as exc:
            from app.core.status import ErrorCode, capture

            self._state.raise_error(
                capture(
                    exc,
                    ErrorCode.AUDIO_PROCESSING_FAILED,
                    user_message="The narration was generated but cannot be played back.",
                    reason="A temporary audio file could not be written.",
                    recommended_action="You can still export the narration to a file.",
                    operation="review_playback",
                )
            )
            return

        self._temp_path = path
        self._player.setSource(QUrl.fromLocalFile(str(path)))

    # -- playback --------------------------------------------------------

    def play_file(self, path: Path) -> None:
        """Play an arbitrary audio file, used for voice previews.

        Must be called on the UI thread: QMediaPlayer starts timers internally
        and silently refuses to work from a worker thread.
        """
        self._player.stop()
        self._player.setSource(QUrl.fromLocalFile(str(path)))
        self._player.play()

    def toggle_play(self) -> None:
        if self._temp_path is None:
            self._state.report("There is no narration to play yet.", "warning")
            return
        if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._player.pause()
        else:
            self._player.play()

    def stop(self) -> None:
        self._player.stop()

    def _seek(self, milliseconds: int) -> None:
        self._player.setPosition(milliseconds)

    def jump_to_caption(self, index: int) -> None:
        segments = self._state.segments
        if 0 <= index < len(segments):
            self._player.setPosition(segments[index].start_ms)

    def next_caption(self) -> None:
        position = self._player.position()
        for segment in self._state.segments:
            if segment.start_ms > position:
                self._player.setPosition(segment.start_ms)
                return

    def previous_caption(self) -> None:
        position = self._player.position()
        previous = 0
        for segment in self._state.segments:
            if segment.start_ms >= position - 250:
                break
            previous = segment.start_ms
        self._player.setPosition(previous)

    def _on_position(self, milliseconds: int) -> None:
        self.waveform.set_position(milliseconds)
        self.timeline.set_position(milliseconds)
        total = self._player.duration() or self.waveform.duration_ms
        self._position.setText(f"{clock(milliseconds)} / {clock(total)}")

        for index, segment in enumerate(self._state.segments):
            if segment.start_ms <= milliseconds < segment.end_ms:
                self._now_playing.setText(f"▶  {segment.text}")
                self.caption_active.emit(index)
                return

    def _on_playback_state(self, playback_state) -> None:
        playing = playback_state == QMediaPlayer.PlaybackState.PlayingState
        self._play.setText("⏸  Pause" if playing else "▶  Play Full Narration")

    def _on_player_error(self, error, message: str) -> None:
        if error == QMediaPlayer.Error.NoError:
            return
        from app.core.status import ErrorCode, OperationError

        self._state.raise_error(
            OperationError(
                ErrorCode.AUDIO_PROCESSING_FAILED,
                "The narration could not be played back.",
                reason=message or "The system audio player reported an error.",
                recommended_action=(
                    "The narration itself is fine — try exporting it and playing "
                    "the exported file."
                ),
                details=f"QMediaPlayer error {error}: {message}",
                operation="review_playback",
            )
        )
