"""Application entry point: ``python -m app``.

Installs a last-resort exception hook so that even a bug nobody anticipated
reaches the user as a readable dialog rather than a silent disappearance.
"""

from __future__ import annotations

import sys
import traceback

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from app.config import APP_NAME
from app.core.status import ErrorCode, OperationError
from app.logging_setup import setup_logging


def _install_exception_hook(window) -> None:
    """Route uncaught exceptions to the error dialog and the log."""
    original = sys.excepthook

    def hook(kind, value, tb) -> None:
        detail = "".join(traceback.format_exception(kind, value, tb))
        try:
            import logging

            logging.getLogger("app").critical("Uncaught exception\n%s", detail)
            window.show_error(
                OperationError(
                    code=ErrorCode.UNKNOWN_ERROR,
                    user_message="Something unexpected went wrong.",
                    reason=f"{kind.__name__}: {value}",
                    recommended_action=(
                        "Your work is autosaved. Try the action again — if it keeps "
                        "happening, use View Technical Details to see what failed."
                    ),
                    details=detail,
                    operation="application",
                )
            )
        except Exception:
            original(kind, value, tb)

    sys.excepthook = hook


def main(argv: list[str] | None = None) -> int:
    settings_verbose = False
    try:
        from app.config import Settings

        settings_verbose = Settings.load().verbose_logging
    except Exception:
        pass
    log_file = setup_logging(verbose=settings_verbose)

    QApplication.setAttribute(Qt.ApplicationAttribute.AA_DontShowIconsInMenus, False)
    application = QApplication(argv if argv is not None else sys.argv)
    application.setApplicationName(APP_NAME)
    application.setOrganizationName(APP_NAME)

    arguments = (argv if argv is not None else sys.argv)[1:]

    # Before building anything: if a copy is already running, give it whatever
    # this launch was asked to open and get out of the way. macOS cannot do this
    # for us here — see app.ui.single_instance.
    from app.ui.single_instance import SingleInstance

    guard = SingleInstance()
    if guard.hand_over(arguments):
        return 0
    guard.listen()

    from app.ui.main_window import MainWindow
    from app.ui.state import AppState
    from app.ui.theme import Appearance, apply_theme

    # Open with the appearance the app was left in; anything unreadable in the
    # settings file must not stop the app from starting.
    try:
        from app.config import Settings as _Settings

        saved = Appearance(_Settings.load().appearance)
    except Exception:
        saved = Appearance.DARK
    apply_theme(application, saved)

    state = AppState()
    window = MainWindow(state)
    _install_exception_hook(window)

    import logging

    logging.getLogger(__name__).info("%s started. Log: %s", APP_NAME, log_file)

    # Worker threads must stop before the interpreter tears down, however the
    # app exits -- otherwise Qt aborts with "Destroyed while thread is running".
    from app.ui.workers import wait_for_threads

    application.aboutToQuit.connect(wait_for_threads)

    window.show()

    def open_arguments(items: list[str]) -> None:
        """Open a file passed on the command line, e.g. from "Open With"."""
        if not items:
            return
        from pathlib import Path

        from app.ui.widgets.dropzone import classify

        path = Path(items[0])
        if not path.exists():
            return
        kind = classify(path)
        if kind == "project":
            window.open_project(path)
        elif kind != "unsupported":
            window.import_file(path, kind)

    def on_second_launch(items: list[str]) -> None:
        """Someone clicked the icon again: surface this window rather than a new one."""
        window.show()
        window.raise_()
        window.activateWindow()
        open_arguments(items)

    guard.activated.connect(on_second_launch)
    application.aboutToQuit.connect(guard.close)
    open_arguments(arguments)

    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
