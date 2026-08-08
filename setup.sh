#!/usr/bin/env bash
# Set up PediAid Voice Studio. Safe to re-run.
#
# Creates a virtual environment beside this script and installs the pinned
# dependency set. Nothing is installed globally, and no existing environment on
# the machine is touched.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$HERE/.venv"
PYTHON_MIN="3.12"

info()  { printf '  %s\n' "$*"; }
ok()    { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn()  { printf '  \033[33m!\033[0m %s\n' "$*"; }
fail()  { printf '  \033[31m✗\033[0m %s\n' "$*" >&2; }

printf '\nPediAid Voice Studio — setup\n'
printf -- '----------------------------\n\n'

# --- Python -------------------------------------------------------------
PY=""
for candidate in python3.12 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
        version="$("$candidate" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
        if [ "$(printf '%s\n%s\n' "$PYTHON_MIN" "$version" | sort -V | head -1)" = "$PYTHON_MIN" ]; then
            PY="$candidate"
            break
        fi
    fi
done

if [ -z "$PY" ]; then
    fail "Python $PYTHON_MIN or newer was not found."
    info "Install it with:  brew install python@3.12"
    exit 1
fi
ok "Python $("$PY" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])') ($PY)"

# --- FFmpeg -------------------------------------------------------------
if command -v ffmpeg >/dev/null 2>&1; then
    ok "FFmpeg $(ffmpeg -version 2>/dev/null | head -1 | awk '{print $3}')"
else
    warn "FFmpeg was not found."
    info "Speech cannot be fitted to subtitle timings without it."
    info "Install it with:  brew install ffmpeg"
fi

# --- virtual environment ------------------------------------------------
if [ ! -d "$VENV" ]; then
    info "Creating virtual environment at .venv …"
    "$PY" -m venv "$VENV"
fi
ok "Virtual environment ready"

info "Installing dependencies (this downloads ~1 GB the first time) …"
"$VENV/bin/python" -m pip install --upgrade pip --quiet
"$VENV/bin/python" -m pip install -r "$HERE/requirements.txt" --quiet
ok "Dependencies installed"

# --- verify -------------------------------------------------------------
printf '\nChecking components\n'
"$VENV/bin/python" - <<'PYCHECK'
import importlib, shutil, sys

def check(label, test):
    try:
        detail = test()
    except Exception as exc:
        print(f"  \033[31m✗\033[0m {label}: {exc}")
        return False
    print(f"  \033[32m✓\033[0m {label}{f' {detail}' if detail else ''}")
    return True

check("NumPy", lambda: importlib.import_module("numpy").__version__)
check("soundfile", lambda: importlib.import_module("soundfile").__version__)
check("PyTorch", lambda: importlib.import_module("torch").__version__)
check("Kokoro", lambda: importlib.import_module("kokoro").__version__ if hasattr(importlib.import_module("kokoro"), "__version__") else "installed")
check("PySide6", lambda: importlib.import_module("PySide6").__version__)
check("FFmpeg", lambda: "found" if shutil.which("ffmpeg") else (_ for _ in ()).throw(RuntimeError("not on PATH")))

from pathlib import Path
cache = Path.home() / ".cache" / "huggingface" / "hub" / "models--hexgrad--Kokoro-82M"
voices = sorted(p.stem for p in cache.glob("snapshots/*/voices/*.pt")) if cache.exists() else []
if voices:
    print(f"  \033[32m✓\033[0m Voice models downloaded: {', '.join(voices)}")
else:
    print("  \033[33m!\033[0m No voice model downloaded yet — the first run fetches it (~330 MB).")
PYCHECK

printf '\nRun the tests\n'
"$VENV/bin/python" -m pytest -q 2>&1 | tail -3

cat <<'DONE'

Setup complete.

  Generate narration from any SRT:
    ./.venv/bin/python generate_natural_tts.py path/to/your.srt

  Preview the narration grouping without generating audio:
    ./.venv/bin/python generate_natural_tts.py path/to/your.srt --plan-only

  List available voices:
    ./.venv/bin/python generate_natural_tts.py --list-voices

DONE
