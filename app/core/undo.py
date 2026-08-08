"""Snapshot-based undo/redo.

A subtitle document is small (tens to low hundreds of segments), so storing a
full snapshot per edit costs almost nothing and removes an entire class of bugs
that hand-written inverse operations are prone to. The stack is capped so a long
editing session cannot grow without bound.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Generic, TypeVar

S = TypeVar("S")

DEFAULT_DEPTH = 200


@dataclass
class _Entry(Generic[S]):
    label: str
    state: S


class UndoStack(Generic[S]):
    """Holds labelled snapshots of a document's state."""

    def __init__(self, initial: S, depth: int = DEFAULT_DEPTH) -> None:
        self._undo: list[_Entry[S]] = []
        self._redo: list[_Entry[S]] = []
        self._current = _Entry("Initial state", initial)
        self._depth = depth
        self._listeners: list[Callable[[], None]] = []

    # -- notification ----------------------------------------------------

    def add_listener(self, callback: Callable[[], None]) -> None:
        self._listeners.append(callback)

    def _notify(self) -> None:
        for callback in list(self._listeners):
            callback()

    # -- recording -------------------------------------------------------

    def push(self, label: str, new_state: S) -> None:
        """Record ``new_state`` as the current state, with the old one undoable."""
        self._undo.append(self._current)
        if len(self._undo) > self._depth:
            self._undo.pop(0)
        self._redo.clear()
        self._current = _Entry(label, new_state)
        self._notify()

    # -- navigation ------------------------------------------------------

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    @property
    def undo_label(self) -> str:
        """Label of the action that undo would reverse."""
        return self._current.label if self._undo else ""

    @property
    def redo_label(self) -> str:
        return self._redo[-1].label if self._redo else ""

    @property
    def state(self) -> S:
        return self._current.state

    def undo(self) -> S:
        if not self._undo:
            return self._current.state
        self._redo.append(self._current)
        self._current = self._undo.pop()
        self._notify()
        return self._current.state

    def redo(self) -> S:
        if not self._redo:
            return self._current.state
        self._undo.append(self._current)
        self._current = self._redo.pop()
        self._notify()
        return self._current.state

    def reset(self, state: S, label: str = "Initial state") -> None:
        """Discard all history -- used when a new file is loaded."""
        self._undo.clear()
        self._redo.clear()
        self._current = _Entry(label, state)
        self._notify()
