#!/usr/bin/env bash
# Wrap "Narration Studio.app" in a drag-to-install disk image.
#
#   ./packaging/make_dmg.sh          ->  dist/NarrationStudio-macOS.dmg
#
# The window shows the app beside an Applications shortcut, which is the
# install gesture every Mac user already knows.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST="$HERE/dist"
APP_NAME="Narration Studio"
APP="$DIST/$APP_NAME.app"
VOLUME="$APP_NAME"
DMG="$DIST/NarrationStudio-macOS.dmg"
STAGING="$DIST/dmg"

info() { printf '  %s\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }

if [ ! -d "$APP" ]; then
    echo "No app bundle at: $APP" >&2
    echo "Build one first with ./packaging/build_macos.sh" >&2
    exit 1
fi

printf '\nBuilding the disk image\n'
printf -- '-----------------------\n\n'

rm -rf "$STAGING" "$DMG"
mkdir -p "$STAGING"

info "Staging…"
cp -R "$APP" "$STAGING/"
ln -s /Applications "$STAGING/Applications"

# A short note for anything the app cannot install for the user.
cat > "$STAGING/READ ME FIRST.txt" <<'NOTE'
Narration Studio
================

To install:  drag "Narration Studio" onto the Applications folder.

First launch
------------
macOS will say the app is from an unidentified developer, because it is not
notarised by Apple. Right-click the app and choose Open, then Open again.
You only do this once.

That is everything
------------------
Nothing else to install. The first launch downloads the voice, which takes a
few minutes and happens once; after that it opens straight away and works
offline.

Everything runs on your own Mac. Nothing is uploaded.
NOTE

SIZE_KB=$(du -sk "$STAGING" | cut -f1)
SIZE_MB=$(( SIZE_KB / 1024 + 120 ))   # headroom for the filesystem itself

info "Creating the image…"
hdiutil create -quiet \
    -srcfolder "$STAGING" \
    -volname "$VOLUME" \
    -fs HFS+ \
    -format UDRW \
    -size "${SIZE_MB}m" \
    "$DIST/temp.dmg"

# --- lay the window out -------------------------------------------------
DEVICE=$(hdiutil attach -readwrite -noverify -noautoopen "$DIST/temp.dmg" \
    | grep -E '^/dev/' | head -1 | awk '{print $1}')
sleep 2

if [ -n "${DEVICE:-}" ]; then
    osascript <<APPLESCRIPT >/dev/null 2>&1 || info "Could not style the window (not fatal)"
tell application "Finder"
    tell disk "$VOLUME"
        open
        set current view of container window to icon view
        set toolbar visible of container window to false
        set statusbar visible of container window to false
        set the bounds of container window to {200, 140, 800, 520}
        set theViewOptions to the icon view options of container window
        set arrangement of theViewOptions to not arranged
        set icon size of theViewOptions to 112
        set position of item "$APP_NAME.app" of container window to {150, 175}
        set position of item "Applications" of container window to {450, 175}
        set position of item "READ ME FIRST.txt" of container window to {300, 320}
        close
        open
        update without registering applications
        delay 1
    end tell
end tell
APPLESCRIPT
    sync
    hdiutil detach "$DEVICE" -quiet || hdiutil detach "$DEVICE" -force -quiet
    ok "Window laid out"
fi

info "Compressing…"
hdiutil convert "$DIST/temp.dmg" -quiet -format UDZO -imagekey zlib-level=9 -o "$DMG"
rm -f "$DIST/temp.dmg"
rm -rf "$STAGING"

FINAL=$(du -h "$DMG" | cut -f1)
printf '\n'
ok "Built: $DMG  ($FINAL)"
printf '\n  Double-click it, then drag the app to Applications.\n\n'
