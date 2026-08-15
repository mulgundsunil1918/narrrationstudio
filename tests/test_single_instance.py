"""One running copy, however many times the icon is clicked.

macOS cannot enforce this for us: the bundle is a shell script that hands over
to the Python framework's own app, so LaunchServices never sees Narration
Studio as running and starts a fresh copy on every click. Two copies means two
windows, two sets of worker threads, and two autosaves overwriting each other.
"""

from __future__ import annotations

import hashlib

import pytest
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

from app.ui.single_instance import SingleInstance


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def name(request) -> str:
    """A short socket name unique to the test, so runs cannot collide.

    Short because a local socket becomes a path under the temp directory, and
    macOS caps the whole thing at about 104 characters — a long name fails with
    nothing but "Name error".
    """
    digest = hashlib.sha1(request.node.name.encode()).hexdigest()[:10]
    return f"ns-test-{digest}"


def test_the_real_socket_name_fits_in_a_unix_socket_path():
    """The shipped name must survive the platform's path limit."""
    from app.ui.single_instance import SOCKET_NAME

    assert len(SOCKET_NAME) < 50, (
        "a long name pushes the socket path past the ~104 character limit and "
        "the guard silently stops working"
    )


def pump(milliseconds: int = 400) -> None:
    loop = QEventLoop()
    QTimer.singleShot(milliseconds, loop.quit)
    loop.exec()


def test_the_first_copy_finds_nobody_and_listens(qt_app, name):
    guard = SingleInstance(name)
    try:
        assert guard.hand_over([]) is False
        assert guard.listen() is True
    finally:
        guard.close()


def test_a_second_copy_hands_over_instead_of_starting(qt_app, name):
    first = SingleInstance(name)
    first.listen()
    received: list[list[str]] = []
    first.activated.connect(received.append)

    second = SingleInstance(name)
    try:
        assert second.hand_over(["/tmp/demo.srt"]) is True, (
            "the second copy must defer to the one already running"
        )
        pump()
        assert received == [["/tmp/demo.srt"]]
    finally:
        first.close()


def test_the_file_to_open_reaches_the_running_copy(qt_app, name):
    """Double-clicking an .srt while the app is open must open it there."""
    first = SingleInstance(name)
    first.listen()
    received: list[list[str]] = []
    first.activated.connect(received.append)

    SingleInstance(name).hand_over(["/tmp/a file with spaces.srt"])
    pump()
    try:
        assert received == [["/tmp/a file with spaces.srt"]]
    finally:
        first.close()


def test_a_launch_with_no_arguments_still_raises_the_window(qt_app, name):
    first = SingleInstance(name)
    first.listen()
    received: list[list[str]] = []
    first.activated.connect(received.append)

    SingleInstance(name).hand_over([])
    pump()
    try:
        assert received == [[]], "an empty launch must still wake the running copy"
    finally:
        first.close()


def test_a_stale_socket_does_not_lock_the_user_out(qt_app, name):
    """A crash leaves the socket file behind; the next launch must still start.

    Getting this wrong is worse than the bug it fixes — the app would refuse to
    open at all until someone found and deleted a file they cannot see.
    """
    crashed = SingleInstance(name)
    crashed.listen()
    crashed._server.close()      # gone, without removing the socket file

    survivor = SingleInstance(name)
    try:
        assert survivor.hand_over([]) is False, "nothing is listening any more"
        assert survivor.listen() is True, "a stale socket must not block startup"
    finally:
        survivor.close()


def test_closing_releases_the_name_for_the_next_launch(qt_app, name):
    first = SingleInstance(name)
    assert first.listen()
    first.close()

    second = SingleInstance(name)
    try:
        assert second.hand_over([]) is False
        assert second.listen() is True
    finally:
        second.close()
