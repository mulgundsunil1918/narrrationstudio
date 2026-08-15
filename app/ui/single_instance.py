"""One running copy, however many times the icon is clicked.

macOS normally stops you launching an app twice, but it cannot here. The bundle
is a shell script that hands over to the Python framework's own ``Python.app``,
so the process that ends up running belongs to a different bundle than the one
that was clicked. LaunchServices therefore does not believe Narration Studio is
running, and every click starts another copy — each with its own window, its own
worker threads, and its own autosave writing over the last one's.

The app has to answer that for itself. The first copy listens on a named local
socket; a later one finds it, hands over any file it was asked to open, and
exits without ever building a window.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

logger = logging.getLogger(__name__)

#: Per-user, so two people on the same Mac each get their own copy.
SOCKET_NAME = "narration-studio-single-instance"

#: Long enough to cross a loaded machine, short enough not to delay a real start.
CONNECT_TIMEOUT_MS = 400


class SingleInstance(QObject):
    """Guards against a second copy, and relays what that copy was asked to do."""

    #: A later launch happened; the payload is its command line arguments.
    activated = Signal(list)

    def __init__(self, name: str = SOCKET_NAME, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._name = name
        self._server: QLocalServer | None = None

    # -- the check -------------------------------------------------------

    def hand_over(self, arguments: list[str]) -> bool:
        """Give ``arguments`` to a copy that is already running.

        Returns True when one answered, meaning this process should exit.
        """
        socket = QLocalSocket()
        socket.connectToServer(self._name)
        if not socket.waitForConnected(CONNECT_TIMEOUT_MS):
            return False

        logger.info("Another copy is already running; handing over and exiting")
        socket.write("\n".join(arguments).encode("utf-8"))
        socket.flush()
        socket.waitForBytesWritten(CONNECT_TIMEOUT_MS)
        socket.disconnectFromServer()
        return True

    def listen(self) -> bool:
        """Become the copy that answers. Returns False if the socket is unusable."""
        server = QLocalServer(self)
        # A copy that crashed leaves its socket file behind, and a stale file
        # would otherwise lock the user out of their own app until they found
        # and deleted it. Nothing answered on it a moment ago, so clear it.
        QLocalServer.removeServer(self._name)
        if not server.listen(self._name):
            logger.warning("Could not listen for other copies: %s", server.errorString())
            return False

        server.newConnection.connect(self._on_connection)
        self._server = server
        return True

    # -- serving ---------------------------------------------------------

    def _on_connection(self) -> None:
        if self._server is None:
            return
        socket = self._server.nextPendingConnection()
        if socket is None:
            return
        socket.waitForReadyRead(CONNECT_TIMEOUT_MS)
        payload = bytes(socket.readAll()).decode("utf-8", "replace")
        socket.disconnectFromServer()
        arguments = [line for line in payload.split("\n") if line]
        logger.info("A second launch arrived with %d argument(s)", len(arguments))
        self.activated.emit(arguments)

    def close(self) -> None:
        if self._server is not None:
            self._server.close()
            QLocalServer.removeServer(self._name)
            self._server = None
