"""Local log file (§36).

Subtitle text is treated as potentially sensitive, so
it is only written to the log when the user turns on verbose logging. Everything
else -- segment numbers, durations, speed factors, errors -- is always logged.
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

from app.config import log_dir

LOG_FILENAME = "pediaid-voice-studio.log"
MAX_BYTES = 2 * 1024 * 1024
BACKUP_COUNT = 3

_configured = False


def setup_logging(verbose: bool = False) -> Path:
    """Configure root logging to a rotating file plus stderr. Idempotent."""
    global _configured
    path = log_dir() / LOG_FILENAME

    if _configured:
        set_verbose(verbose)
        return path

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    file_handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s  %(levelname)-7s %(name)-28s %(message)s")
    )
    root.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(logging.INFO)
    stream_handler.setFormatter(logging.Formatter("%(levelname)-7s %(name)s: %(message)s"))
    root.addHandler(stream_handler)

    _configured = True
    return path


def set_verbose(verbose: bool) -> None:
    """Raise or lower file-log detail at runtime."""
    for handler in logging.getLogger().handlers:
        if isinstance(handler, logging.handlers.RotatingFileHandler):
            handler.setLevel(logging.DEBUG if verbose else logging.INFO)


def log_path() -> Path:
    return log_dir() / LOG_FILENAME


def redact(text: str, verbose: bool) -> str:
    """Return text for logging: the real thing when verbose, a summary otherwise."""
    if verbose:
        return text
    return f"<{len(text)} chars>"
