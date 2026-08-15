"""Test-wide isolation from the real installation.

Several tests build an ``AppState`` or a whole ``MainWindow``, and those read
and write the per-user directory the installed app uses: settings, terminology
rules, recent projects, the audio cache and the crash-recovery autosave. Sharing
that with a real installation is a bug in both directions — a test run can
overwrite someone's work, and their work can hang a test run. The latter is not
hypothetical: a pending recovery file makes ``MainWindow`` open a modal asking
whether to restore it, and a headless test then waits forever for a click.

Redirecting the data directory before anything imports the app fixes the whole
class at once.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

# Set at import, before any test module pulls in app.config: the directory is
# read on each call, but a module-scoped fixture would run too late for tests
# that touch it while collecting.
_SANDBOX = Path(tempfile.mkdtemp(prefix="narration-studio-tests-"))
os.environ["NARRATION_STUDIO_DATA_DIR"] = str(_SANDBOX)

import pytest  # noqa: E402


@pytest.fixture(scope="session")
def data_sandbox() -> Path:
    """The throwaway directory standing in for the user's app-support folder."""
    return _SANDBOX


@pytest.fixture(scope="session", autouse=True)
def _guard_real_data():
    """Fail loudly if anything escapes the sandbox back to the real directory."""
    from app.config import support_dir

    resolved = support_dir().resolve()
    assert _SANDBOX.resolve() in resolved.parents or resolved == _SANDBOX.resolve(), (
        f"tests are writing to {resolved}, not the sandbox"
    )
    yield
