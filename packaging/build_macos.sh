#!/usr/bin/env bash
# Build "Narration Studio.app" for macOS.
#
#   ./packaging/build_macos.sh              -> dist/Narration Studio.app
#   ./packaging/build_macos.sh --install    -> also copies it to /Applications
#
# The bundle carries the application source and a launcher. The Python runtime
# is created on first launch in ~/Library/Application Support, not inside the
# bundle: PyTorch alone is ~350 MB and a self-contained bundle would be several
# gigabytes -- too large for a GitHub release, and it would need re-signing on
# every dependency change.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST="$HERE/dist"
APP_NAME="Narration Studio"
BUNDLE="$DIST/$APP_NAME.app"
BUNDLE_ID="com.narrationstudio.app"
VERSION="$(grep -m1 '^version' "$HERE/pyproject.toml" | cut -d'"' -f2)"
VERSION="${VERSION:-0.1.0}"

info() { printf '  %s\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }

printf '\nBuilding %s %s\n' "$APP_NAME" "$VERSION"
printf -- '--------------------------------\n\n'

rm -rf "$BUNDLE"
mkdir -p "$BUNDLE/Contents/MacOS" "$BUNDLE/Contents/Resources"

# --- application source -------------------------------------------------
info "Copying application source…"
rsync -a --quiet \
    --exclude '__pycache__' --exclude '*.pyc' --exclude '.venv' \
    --exclude 'dist' --exclude 'build' \
    "$HERE/app" "$BUNDLE/Contents/Resources/"
cp "$HERE/requirements.txt" "$BUNDLE/Contents/Resources/"
cp "$HERE/generate_natural_tts.py" "$BUNDLE/Contents/Resources/"
[ -f "$HERE/README.md" ] && cp "$HERE/README.md" "$BUNDLE/Contents/Resources/"
ok "Source copied"

# --- icon ---------------------------------------------------------------
if [ -x "$HERE/.venv/bin/python" ]; then
    info "Drawing the icon…"
    "$HERE/.venv/bin/python" "$HERE/packaging/make_icon.py" "$DIST/icon" >/dev/null 2>&1 || true
    if [ -f "$DIST/icon/AppIcon.icns" ]; then
        cp "$DIST/icon/AppIcon.icns" "$BUNDLE/Contents/Resources/AppIcon.icns"
        ok "Icon built"
    fi
fi

# --- Info.plist ---------------------------------------------------------
cat > "$BUNDLE/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>$APP_NAME</string>
    <key>CFBundleDisplayName</key><string>$APP_NAME</string>
    <key>CFBundleIdentifier</key><string>$BUNDLE_ID</string>
    <key>CFBundleVersion</key><string>$VERSION</string>
    <key>CFBundleShortVersionString</key><string>$VERSION</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleExecutable</key><string>NarrationStudio</string>
    <key>CFBundleIconFile</key><string>AppIcon</string>
    <key>NSHighResolutionCapable</key><true/>
    <key>LSMinimumSystemVersion</key><string>12.0</string>
    <!-- Belt and braces against a second copy. It is not sufficient on its own:
         the launcher hands over to the Python framework's own bundle, so
         LaunchServices does not recognise the running process as this app. The
         app guards itself in app/ui/single_instance.py. -->
    <key>LSMultipleInstancesProhibited</key><true/>
    <key>NSHumanReadableCopyright</key><string>Runs entirely on this Mac.</string>
    <key>CFBundleDocumentTypes</key>
    <array>
      <dict>
        <key>CFBundleTypeName</key><string>Subtitle file</string>
        <key>CFBundleTypeRole</key><string>Editor</string>
        <key>LSItemContentTypes</key>
        <array><string>public.srt</string><string>public.plain-text</string></array>
        <key>CFBundleTypeExtensions</key>
        <array><string>srt</string><string>narration</string></array>
      </dict>
    </array>
</dict>
</plist>
PLIST
ok "Info.plist written"

# --- launcher -----------------------------------------------------------
cat > "$BUNDLE/Contents/MacOS/NarrationStudio" <<'LAUNCHER'
#!/usr/bin/env bash
# Prepares the Python runtime on first launch, then starts the app.
set -uo pipefail

RES="$(cd "$(dirname "${BASH_SOURCE[0]}")/../Resources" && pwd)"
SUPPORT="$HOME/Library/Application Support/Narration Studio"
RUNTIME="$SUPPORT/runtime"
PYTHON="$RUNTIME/bin/python"
STAMP="$RUNTIME/.installed"

mkdir -p "$SUPPORT"

# The stamp records which requirements the runtime was built from. A plain
# "already installed" flag would leave everyone who upgrades on the old set of
# packages, with a new feature that silently does nothing.
WANTED="$(/usr/bin/shasum -a 256 "$RES/requirements.txt" 2>/dev/null | cut -d' ' -f1)"
HAVE="$(cat "$STAMP" 2>/dev/null || true)"

dialog() {  # title, message
    /usr/bin/osascript -e "display dialog \"$2\" with title \"$1\" buttons {\"OK\"} default button 1" >/dev/null 2>&1
}

usable() {  # a real Python 3.12+ at this exact path
    [ -x "$1" ] || return 1
    "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)' 2>/dev/null
}

find_python() {
    # Absolute paths first, and deliberately so. An app launched from Finder
    # gets PATH=/usr/bin:/bin:/usr/sbin:/sbin — no /usr/local/bin, no Homebrew,
    # no python.org framework — and the only python3 on it is macOS's own 3.9.
    # Searching PATH alone therefore told people to install a Python they had
    # already installed.
    # 3.12 first, and that order matters: torch 2.2.2 publishes no wheels past
    # cp312, so building the runtime on a newer Python installs nothing and
    # leaves the user staring at a wall of pip errors. Newer versions are still
    # tried, but only when 3.12 is genuinely absent.
    for version in 3.12 3.13 3.14; do
        for candidate in \
            "/Library/Frameworks/Python.framework/Versions/$version/bin/python$version" \
            "/opt/homebrew/opt/python@$version/bin/python$version" \
            "/opt/homebrew/bin/python$version" \
            "/usr/local/opt/python@$version/bin/python$version" \
            "/usr/local/bin/python$version" \
            "$HOME/.pyenv/versions/$version"*/bin/python3
        do
            if usable "$candidate"; then echo "$candidate"; return 0; fi
        done
    done

    for candidate in \
        "/Library/Frameworks/Python.framework/Versions/Current/bin/python3" \
        "/opt/homebrew/bin/python3" \
        "/usr/local/bin/python3"
    do
        if usable "$candidate"; then echo "$candidate"; return 0; fi
    done

    # Only now fall back to whatever the environment offers.
    for name in python3.12 python3.13 python3.14 python3; do
        resolved="$(command -v "$name" 2>/dev/null)" || continue
        if usable "$resolved"; then echo "$resolved"; return 0; fi
    done
    return 1
}

if [ "$HAVE" != "$WANTED" ] || [ ! -x "$PYTHON" ]; then
    HOST_PYTHON="$(find_python)" || {
        dialog "Narration Studio" "Python 3.12 or newer is needed, and none was found on this Mac.\n\nDownload it from python.org/downloads, run the installer, then open Narration Studio again.\n\n(If you believe you already have it, it may be installed somewhere unusual — open the app from Terminal to see the search.)"
        exit 1
    }

    if [ -x "$PYTHON" ]; then
        HEADLINE="updating"
        EXPLAIN="This version needs a few extra components. Only what changed is downloaded."
    else
        HEADLINE="first-time setup"
        EXPLAIN="This downloads the speech engine (about 2 GB) and runs once."
    fi

    # Needs several minutes and a few GB. Do it in Terminal so the user can see
    # progress rather than staring at a bouncing dock icon.
    SETUP="$SUPPORT/first-run-setup.sh"
    cat > "$SETUP" <<SETUP_EOF
#!/usr/bin/env bash
echo ""
echo "Narration Studio — $HEADLINE"
echo "-----------------------------------"
echo "$EXPLAIN"
echo ""

# The stamp is written only after everything succeeded. A half-built runtime
# with a stamp on it would look installed and then fail on every launch.
fail() {
    echo ""
    echo "Setup did not finish: \$1"
    echo ""
    echo "Nothing is broken — opening Narration Studio again will retry."
    echo "Press Return to close this window."
    read -r _
    exit 1
}

if [ ! -x "$PYTHON" ]; then
    "$HOST_PYTHON" -m venv "$RUNTIME" || fail "the Python environment could not be created."
fi
"$RUNTIME/bin/python" -m pip install --upgrade pip --quiet
"$RUNTIME/bin/python" -m pip install -r "$RES/requirements.txt" \
    || fail "some components could not be downloaded. Check your internet connection."
printf '%s' "$WANTED" > "$STAMP"
echo ""
echo "Ready. Opening Narration Studio…"
sleep 1
open -a "Narration Studio" || true
SETUP_EOF
    chmod +x "$SETUP"
    open -a Terminal "$SETUP"
    exit 0
fi

cd "$RES"
exec "$PYTHON" -m app "$@"
LAUNCHER
chmod +x "$BUNDLE/Contents/MacOS/NarrationStudio"
ok "Launcher written"

# --- sign locally so Gatekeeper is less hostile -------------------------
if command -v codesign >/dev/null 2>&1; then
    codesign --force --deep --sign - "$BUNDLE" >/dev/null 2>&1 \
        && ok "Ad-hoc signed" \
        || info "Could not sign (not fatal)"
fi

SIZE="$(du -sh "$BUNDLE" | cut -f1)"
printf '\n'
ok "Built: $BUNDLE  ($SIZE)"

if [ "${1:-}" = "--install" ]; then
    info "Installing to /Applications…"
    rm -rf "/Applications/$APP_NAME.app"
    cp -R "$BUNDLE" "/Applications/"
    ok "Installed to /Applications/$APP_NAME.app"
fi

cat <<DONE

Next:
  • Double-click "$BUNDLE"
  • Or install it:  ./packaging/build_macos.sh --install

The first launch opens Terminal to download the speech engine (~2 GB, a few
minutes). Every launch after that is immediate.

DONE
