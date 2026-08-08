"""Generation: pre-flight, progress, and every way it can end.

The screen is a state machine with one visible state at all times. It cannot
show a spinner with nothing behind it: the worker reports progress, a watchdog
notices a stall, cancellation is real, and a failed segment is listed rather
than swallowed.
"""

from __future__ import annotations

import time

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QProgressBar,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.core.preflight import PreflightReport, run_preflight
from app.core.status import OperationError, OperationState
from app.pipeline import GenerationOutcome, GroupProgress, derive_output_path
from app.ui.state import AppState
from app.ui.theme import palette, tone
from app.ui.widgets.common import (
    Card,
    Divider,
    GhostButton,
    Metric,
    Pill,
    PrimaryButton,
    SecondaryButton,
    Spinner,
    caption,
    clear_layout,
    clock,
    heading,
    label,
    muted,
    section_label,
    title,
)
from app.ui.widgets.waveform import WaveformView
from app.ui.workers import GenerationWorker, run_in_thread

STALL_CHECK_MS = 5000


class GenerateScreen(QWidget):
    """Pre-flight → generate → completed / warning / error / cancelled."""

    finished = Signal(object)          # GenerationOutcome
    change_voice_requested = Signal()

    def __init__(self, state: AppState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._state = state
        self._worker: GenerationWorker | None = None
        self._thread = None
        self._started_at = 0.0
        self._done = 0
        self._total = 0
        self._report: PreflightReport | None = None
        self._failures: list[OperationError] = []

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

        self._column.addWidget(self._build_preflight())
        self._column.addWidget(self._build_progress())
        self._column.addWidget(self._build_issues())
        self._scroll.setWidget(holder)
        outer.addWidget(self._scroll, 1)

        self._ticker = QTimer(self)
        self._ticker.setInterval(1000)
        self._ticker.timeout.connect(self._tick)

        self._watchdog = QTimer(self)
        self._watchdog.setInterval(STALL_CHECK_MS)
        self._watchdog.timeout.connect(self._check_stall)

        state.project_changed.connect(self.reset)
        self.reset()

    # -- construction ----------------------------------------------------

    def _build_header(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("TopBar")
        row = QHBoxLayout(bar)
        row.setContentsMargins(28, 16, 28, 16)
        row.setSpacing(12)
        column = QVBoxLayout()
        column.setSpacing(2)
        column.addWidget(title("Generate"))
        self._subtitle = muted("Check everything is ready, then create the narration.")
        column.addWidget(self._subtitle)
        row.addLayout(column)
        row.addStretch(1)
        self._state_pill = Pill("Ready", "neutral")
        row.addWidget(self._state_pill)
        return bar

    def _build_preflight(self) -> QWidget:
        self._preflight_card = Card()
        self._preflight_card.add(section_label("Pre-flight check"))
        self._checks_holder = QVBoxLayout()
        self._checks_holder.setSpacing(7)
        self._preflight_card.add_layout(self._checks_holder)

        self._preflight_card.add(Divider())
        actions = QHBoxLayout()
        actions.setSpacing(9)
        self._verdict = heading("")
        actions.addWidget(self._verdict)
        actions.addStretch(1)

        self._recheck = SecondaryButton("Re-check")
        self._recheck.clicked.connect(self.run_checks)
        actions.addWidget(self._recheck)

        self._fix = SecondaryButton("Choose Another Voice")
        self._fix.clicked.connect(self.change_voice_requested)
        self._fix.setVisible(False)
        actions.addWidget(self._fix)

        self._generate = PrimaryButton("Generate Narration")
        self._generate.clicked.connect(self.start)
        actions.addWidget(self._generate)
        self._preflight_card.add_layout(actions)
        return self._preflight_card

    def _build_progress(self) -> QWidget:
        self._progress_card = Card()
        self._progress_card.setVisible(False)

        header = QHBoxLayout()
        header.setSpacing(10)
        self._spinner = Spinner(18)
        header.addWidget(self._spinner)
        self._status = heading("Generating your narration…")
        header.addWidget(self._status)
        header.addStretch(1)
        self._cancel = SecondaryButton("Cancel")
        self._cancel.clicked.connect(self.cancel)
        header.addWidget(self._cancel)
        self._progress_card.add_layout(header)

        self._bar = QProgressBar()
        self._bar.setRange(0, 100)
        self._bar.setValue(0)
        self._progress_card.add(self._bar)

        metrics = QHBoxLayout()
        metrics.setSpacing(28)
        self._m_segment = Metric("—", "SEGMENT")
        self._m_percent = Metric("0%", "COMPLETE")
        self._m_elapsed = Metric("0:00", "ELAPSED")
        self._m_remaining = Metric("—", "REMAINING")
        for metric in (self._m_segment, self._m_percent, self._m_elapsed, self._m_remaining):
            metrics.addWidget(metric)
        metrics.addStretch(1)
        self._progress_card.add_layout(metrics)

        self._progress_card.add(Divider())
        self._progress_card.add(section_label("Currently speaking"))
        self._current = muted("", wrap=True)
        self._progress_card.add(self._current)

        self._first_note = muted("", wrap=True)
        self._first_note.setVisible(False)
        self._progress_card.add(self._first_note)

        self._stall_note = muted("", wrap=True)
        self._stall_note.setVisible(False)
        self._progress_card.add(self._stall_note)

        self.waveform = WaveformView()
        self.waveform.setVisible(False)
        self._progress_card.add(self.waveform)
        return self._progress_card

    def _build_issues(self) -> QWidget:
        self._issues_card = Card()
        self._issues_card.setVisible(False)
        self._issues_card.add(section_label("Segments that need attention"))
        self._issues_holder = QVBoxLayout()
        self._issues_holder.setSpacing(8)
        self._issues_card.add_layout(self._issues_holder)

        row = QHBoxLayout()
        row.setSpacing(9)
        row.addStretch(1)
        self._retry_failed = SecondaryButton("Retry Failed Segments")
        self._retry_failed.clicked.connect(self.retry_failed)
        row.addWidget(self._retry_failed)
        self._issues_card.add_layout(row)
        return self._issues_card

    # -- pre-flight ------------------------------------------------------

    def reset(self) -> None:
        self._progress_card.setVisible(False)
        self._issues_card.setVisible(False)
        self._failures = []
        self.run_checks()

    def run_checks(self) -> None:
        clear_layout(self._checks_holder)

        if not self._state.has_captions:
            self._verdict.setText("No script loaded")
            self._generate.setEnabled(False)
            self._fix.setVisible(False)
            self._state_pill.set_status("No script", "neutral")
            self._checks_holder.addWidget(
                muted("Import a subtitle file from Home to begin.", wrap=True)
            )
            return

        self._state.set_state(OperationState.VALIDATING)
        output = self._output_path()
        self._report = run_preflight(
            self._state.segments,
            self._state.voice.engine,
            self._state.voice.voice,
            output,
            self._state.timeline_ms,
        )

        for check in self._report.checks:
            self._checks_holder.addWidget(self._check_row(check))

        if self._report.passed:
            self._verdict.setText("Ready to generate")
            self._verdict.setStyleSheet(f"color: {palette().success};")
            self._generate.setEnabled(True)
            self._fix.setVisible(False)
            self._state.set_state(OperationState.READY)
            self._state_pill.set_status("Ready", "success")
        else:
            failed = self._report.failures[0]
            self._verdict.setText(f"Cannot generate yet — {failed.label.lower()}")
            self._verdict.setStyleSheet(f"color: {palette().danger};")
            self._generate.setEnabled(False)
            self._fix.setVisible(failed.key == "voice")
            self._state.set_state(OperationState.ERROR)
            self._state_pill.set_status("Blocked", "error")

    def _check_row(self, check) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        foreground, _ = tone("success" if check.passed else "error")
        mark = label(check.mark)
        mark.setFixedWidth(16)
        mark.setStyleSheet(f"color: {foreground}; font-weight: 700;")
        layout.addWidget(mark)
        layout.addWidget(label(check.label, "Body"))
        layout.addStretch(1)
        if check.detail:
            layout.addWidget(muted(check.detail))

        if not check.passed and check.error:
            button = GhostButton("Details")
            button.clicked.connect(
                lambda _c=False, e=check.error: self._state.error_raised.emit(e)
            )
            layout.addWidget(button)
        return row

    def _output_path(self):
        source = self._state.document.source_path
        if source is not None:
            return derive_output_path(source)
        if self._state.project_path is not None:
            return self._state.project_path.with_suffix(".wav")
        return None

    # -- generation ------------------------------------------------------

    def start(self, only_groups: list[int] | None = None) -> None:
        if self._state.is_busy:
            self._state.report("A generation is already running.", "warning")
            return
        if not self._state.has_captions:
            self._state.report("There is no script to narrate.", "error")
            return

        self._total = len(self._state.plan())
        self._done = 0
        self._failures = []
        self._started_at = time.monotonic()

        self._progress_card.setVisible(True)
        self._issues_card.setVisible(False)
        self._generate.setEnabled(False)
        self._recheck.setEnabled(False)
        self._cancel.setEnabled(True)
        self._cancel.setText("Cancel")
        self._spinner.start()
        self._stall_note.setVisible(False)
        self.waveform.setVisible(False)
        self._bar.setRange(0, 0)          # indeterminate until the first result
        self._m_percent.set_value("—")
        self._m_remaining.set_value("estimating…")
        self._first_note.setVisible(False)
        self._status.setText("Generating your narration…")
        self._state.set_state(OperationState.GENERATING)
        self._state_pill.set_status("Generating", "info")

        worker = GenerationWorker(
            self._state.segments, self._state.generation_settings(), only_groups
        )
        worker.progress.connect(self._on_progress, Qt.ConnectionType.QueuedConnection)
        worker.finished.connect(self._on_finished, Qt.ConnectionType.QueuedConnection)
        worker.failed.connect(self._on_failed, Qt.ConnectionType.QueuedConnection)
        worker.cancelled.connect(self._on_cancelled, Qt.ConnectionType.QueuedConnection)
        worker.stalled.connect(self._on_stalled, Qt.ConnectionType.QueuedConnection)
        self._worker = worker
        self._thread = run_in_thread(worker, self)

        self._ticker.start()
        self._watchdog.start()

    def cancel(self) -> None:
        if self._worker is None:
            return
        self._worker.cancel()
        self._cancel.setEnabled(False)
        self._cancel.setText("Cancelling…")
        self._status.setText("Finishing the current segment, then stopping…")
        self._state.report("Cancelling — the current segment will finish first.", "info")

    def retry_failed(self) -> None:
        indices = [f.segment - 1 for f in self._failures if f.segment]
        if not indices:
            self._state.report("There are no failed segments to retry.", "info")
            return
        self._state.report(f"Retrying {len(indices)} segment(s)…", "info")
        self.start(only_groups=indices)

    # -- worker callbacks ------------------------------------------------

    def _on_progress(self, item: GroupProgress) -> None:
        if item.starting:
            self._current.setText(f"“{item.text[:200]}”")
            self._m_segment.set_value(f"{item.index + 1} / {item.total}")
            if self._done == 0:
                # Nothing has completed yet, so there is no honest percentage.
                # An animated bar says "working"; a static 0% says "stuck".
                self._bar.setRange(0, 0)
                self._m_percent.set_value("—")
                self._m_remaining.set_value("estimating…")
                self._first_note.setText(
                    "The first segment also loads the voice model, so it takes "
                    "longer than the rest."
                )
                self._first_note.setVisible(True)
            return

        self._done += 1
        if self._bar.maximum() == 0:
            self._bar.setRange(0, 100)
        self._first_note.setVisible(False)
        if item.failed and item.error:
            self._failures.append(item.error)

        percent = int(self._done / max(1, self._total) * 100)
        self._bar.setValue(percent)
        self._m_percent.set_value(f"{percent}%")
        self._m_remaining.set_value("—" if self._done >= self._total else "estimating…")
        self._m_segment.set_value(f"{item.index + 1} / {item.total}")
        self._stall_note.setVisible(False)

    def _tick(self) -> None:
        elapsed = time.monotonic() - self._started_at
        self._m_elapsed.set_value(clock(int(elapsed * 1000)))
        if self._done > 0 and self._total > 0:
            per = elapsed / self._done
            remaining = per * max(0, self._total - self._done)
            self._m_remaining.set_value(clock(int(remaining * 1000)))

    def _check_stall(self) -> None:
        if self._worker is not None:
            self._worker.check_stall()

    def _on_stalled(self, seconds: float) -> None:
        self._stall_note.setVisible(True)
        self._stall_note.setText(
            f"Generation is taking longer than expected — no progress for "
            f"{int(seconds)} seconds. A long segment or a first-time model "
            "download can cause this. You can keep waiting or cancel."
        )
        self._stall_note.setStyleSheet(f"color: {palette().warning};")
        self._state_pill.set_status("Taking longer than expected", "warning")

    def _stop_timers(self) -> None:
        if self._bar.maximum() == 0:
            self._bar.setRange(0, 100)
        self._first_note.setVisible(False)
        self._ticker.stop()
        self._watchdog.stop()
        self._spinner.stop()
        self._worker = None
        self._recheck.setEnabled(True)
        self._generate.setEnabled(True)

    def _on_finished(self, outcome: GenerationOutcome) -> None:
        self._stop_timers()
        self._bar.setRange(0, 100)
        self._bar.setValue(100)
        self._m_percent.set_value("100%")
        self._m_remaining.set_value("—")
        self._first_note.setVisible(False)
        self._cancel.setVisible(False)
        self.waveform.setVisible(True)
        self.waveform.set_audio(outcome.audio, outcome.sample_rate)

        self._failures = list(outcome.failures)
        self._render_issues(outcome)

        if outcome.failures:
            self._status.setText(
                f"Finished with {len(outcome.failures)} failed segment(s)."
            )
            self._state_pill.set_status("Needs attention", "warning")
        else:
            self._status.setText("Narration generated successfully.")
            self._state_pill.set_status("Completed", "success")

        self._state.set_outcome(outcome)
        self.finished.emit(outcome)

    def _on_failed(self, error: OperationError) -> None:
        self._stop_timers()
        self._cancel.setVisible(False)
        self._status.setText("Generation failed.")
        self._state_pill.set_status("Failed", "error")
        self._state.raise_error(error)

    def _on_cancelled(self, outcome: GenerationOutcome | None) -> None:
        self._stop_timers()
        self._cancel.setVisible(False)
        self._status.setText("Generation cancelled.")
        self._state_pill.set_status("Cancelled", "neutral")
        self._state.set_state(OperationState.CANCELLED)
        completed = outcome.completed_groups if outcome else 0
        self._state.report(
            f"Generation cancelled. {completed} segment(s) were kept and are cached, "
            "so restarting will be faster.",
            "info",
        )
        if outcome is not None and completed:
            self.waveform.setVisible(True)
            self.waveform.set_audio(outcome.audio, outcome.sample_rate)

    def _render_issues(self, outcome: GenerationOutcome) -> None:
        clear_layout(self._issues_holder)

        rows: list[QWidget] = []
        for failure in outcome.failures:
            rows.append(self._issue_row(failure.user_message, failure.reason, "error", failure))
        for text in outcome.warnings[:20]:
            rows.append(self._issue_row(text, "", "warning", None))

        if not rows:
            self._issues_card.setVisible(False)
            return

        for row in rows:
            self._issues_holder.addWidget(row)
        self._retry_failed.setVisible(bool(outcome.failures))
        self._issues_card.setVisible(True)

    def _issue_row(self, headline: str, detail: str, kind: str, error) -> QWidget:
        card = Card(quiet=True)
        card.body.setContentsMargins(14, 11, 14, 12)
        card.body.setSpacing(5)

        top = QHBoxLayout()
        top.setSpacing(9)
        foreground, _ = tone(kind)
        mark = label("✕" if kind == "error" else "⚠")
        mark.setFixedWidth(16)
        mark.setStyleSheet(f"color: {foreground}; font-weight: 700;")
        top.addWidget(mark)
        top.addWidget(label(headline, "Body", wrap=True), 1)
        if error is not None:
            button = GhostButton("Details")
            button.clicked.connect(
                lambda _c=False, e=error: self._state.error_raised.emit(e)
            )
            top.addWidget(button)
        card.add_layout(top)
        if detail:
            card.add(caption(detail, wrap=True))
        return card
