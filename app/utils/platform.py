"""Platform differences, isolated here rather than scattered through the UI.

The app runs on macOS and Windows. Everything that genuinely differs between
them — where files live, how to reveal one in the file manager, which monospace
font exists — is decided once, in this module.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

IS_MACOS = sys.platform == "darwin"
IS_WINDOWS = sys.platform.startswith("win")


def file_manager_name() -> str:
    """What to call the file manager in button labels and messages."""
    if IS_MACOS:
        return "Finder"
    if IS_WINDOWS:
        return "File Explorer"
    return "the file manager"


def reveal(path: Path) -> tuple[bool, str]:
    """Show ``path`` in the system file manager.

    Returns ``(ok, reason)`` instead of raising: failing to open a file browser
    is a minor inconvenience, not something worth an error dialog, but it still
    must not fail silently.
    """
    try:
        if IS_MACOS:
            subprocess.run(["open", "-R", str(path)], check=True, capture_output=True)
        elif IS_WINDOWS:
            # explorer returns a non-zero exit code even when it succeeds.
            subprocess.run(["explorer", "/select,", str(path)], capture_output=True)
        else:
            subprocess.run(
                ["xdg-open", str(path.parent)], check=True, capture_output=True
            )
    except FileNotFoundError:
        return False, f"{file_manager_name()} could not be launched."
    except subprocess.CalledProcessError as exc:
        return False, exc.stderr.decode("utf-8", "replace").strip() or "unknown error"
    except Exception as exc:  # never let this break the calling screen
        logger.warning("Could not reveal %s: %s", path, exc)
        return False, str(exc)
    return True, ""


def monospace_families() -> list[str]:
    """Fixed-width fonts to try, best first, for the platform in use."""
    if IS_MACOS:
        return ["SF Mono", "Menlo", "Monaco", "Courier New"]
    if IS_WINDOWS:
        return ["Cascadia Mono", "Consolas", "Courier New"]
    return ["DejaVu Sans Mono", "Liberation Mono", "Courier New"]


def data_root(app_name: str, app_id: str, kind: str) -> Path:
    """Per-user directory for ``kind`` in ("support", "cache", "logs")."""
    home = Path.home()

    if IS_MACOS:
        return {
            "support": home / "Library" / "Application Support" / app_name,
            "cache": home / "Library" / "Caches" / app_id,
            "logs": home / "Library" / "Logs" / app_name,
        }[kind]

    if IS_WINDOWS:
        roaming = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        return {
            "support": roaming / app_name,
            "cache": local / app_name / "Cache",
            "logs": local / app_name / "Logs",
        }[kind]

    xdg_data = Path(os.environ.get("XDG_DATA_HOME", home / ".local" / "share"))
    xdg_cache = Path(os.environ.get("XDG_CACHE_HOME", home / ".cache"))
    return {
        "support": xdg_data / app_id,
        "cache": xdg_cache / app_id,
        "logs": xdg_data / app_id / "logs",
    }[kind]
