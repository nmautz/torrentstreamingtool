#!/usr/bin/env bash
#
# build-ipa.sh — produce an UNSIGNED StreamLink .ipa for sideloading.
#
# The output is intentionally unsigned: a re-signing sideloader (Sideloadly,
# AltStore, ESign, …) re-signs it with your free Apple ID on-device, so no Mac
# is needed for the weekly 7-day refresh. The .ipa will NOT install directly.
#
# By DEFAULT this does everything needed to build correctly after a `git pull`:
#   1. npm install            — installs/updates the Capacitor CLI + core
#   2. re-vendor capacitor.js — copies @capacitor/core's web runtime into www/
#                               (keeps it version-matched; a no-bundler page needs
#                               it to reach native plugins — see docs/GOTCHAS.md)
#   3. npx cap sync ios       — copies www/ into the app AND updates native deps
#   4. xcodebuild + package the unsigned .ipa
#
# Usage:
#   ./build-ipa.sh                 # full, update-safe build (recommended)
#   ./build-ipa.sh --fast          # skip deps + sync (web/native already current)
#   ./build-ipa.sh --no-deps       # skip npm install (keep sync)
#   ./build-ipa.sh --no-sync       # skip cap sync   (keep npm install)  [alias: --no-copy]
#
# Requires: Xcode + the iOS device platform component, Node/npm.

set -euo pipefail

# Resolve repo-relative paths so the script works from any CWD.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$SCRIPT_DIR/ios/App"
DERIVED="$APP_DIR/build"
PRODUCTS="$DERIVED/Build/Products/Release-iphoneos"
IPA_NAME="StreamLink-unsigned.ipa"
OUT_IPA="$SCRIPT_DIR/$IPA_NAME"
CORE_RUNTIME="$SCRIPT_DIR/node_modules/@capacitor/core/dist/capacitor.js"
VENDORED_RUNTIME="$SCRIPT_DIR/www/capacitor.js"

DO_DEPS=1
DO_SYNC=1
for arg in "$@"; do
  case "$arg" in
    --no-sync|--no-copy) DO_SYNC=0 ;;
    --no-deps)           DO_DEPS=0 ;;
    --fast)              DO_DEPS=0; DO_SYNC=0 ;;
    -h|--help)           sed -n '2,30p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

cd "$SCRIPT_DIR"

# 1. Dependencies — required for `npx cap` and for re-vendoring the runtime.
if [[ "$DO_DEPS" -eq 1 ]]; then
  echo "==> Installing/refreshing Node dependencies (npm install)"
  npm install
elif [[ ! -d "$SCRIPT_DIR/node_modules" ]]; then
  echo "ERROR: --no-deps/--fast given but node_modules/ is missing. Run 'npm install' first." >&2
  exit 1
fi

# 2. Re-vendor the Capacitor web runtime so www/capacitor.js can never drift from
#    the installed @capacitor/core (the cause of "plugin not registered" errors).
if [[ -f "$CORE_RUNTIME" ]]; then
  if ! cmp -s "$CORE_RUNTIME" "$VENDORED_RUNTIME" 2>/dev/null; then
    echo "==> Updating vendored web runtime (www/capacitor.js) from @capacitor/core"
    cp "$CORE_RUNTIME" "$VENDORED_RUNTIME"
  else
    echo "==> Vendored web runtime already up to date"
  fi
else
  echo "WARNING: $CORE_RUNTIME not found; cannot verify www/capacitor.js is current." >&2
fi

# 3. Sync web assets + native deps into the iOS project.
if [[ "$DO_SYNC" -eq 1 ]]; then
  echo "==> Syncing into the iOS project (npx cap sync ios)"
  npx cap sync ios
else
  echo "==> Skipping cap sync (--no-sync/--fast)"
fi

# 4. Build the unsigned Release app for device.
echo "==> Building unsigned Release app for device"
xcodebuild \
  -project "$APP_DIR/App.xcodeproj" \
  -scheme App \
  -configuration Release \
  -destination 'generic/platform=iOS' \
  -derivedDataPath "$DERIVED" \
  CODE_SIGNING_ALLOWED=NO \
  CODE_SIGNING_REQUIRED=NO \
  CODE_SIGN_IDENTITY="" \
  build

APP_BUNDLE="$PRODUCTS/App.app"
if [[ ! -d "$APP_BUNDLE" ]]; then
  echo "ERROR: build succeeded but $APP_BUNDLE is missing." >&2
  exit 1
fi

echo "==> Packaging $IPA_NAME"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
mkdir "$WORK/Payload"
cp -R "$APP_BUNDLE" "$WORK/Payload/"
rm -f "$OUT_IPA"
( cd "$WORK" && zip -qr9 "$OUT_IPA" Payload )

BUNDLE_ID="$(/usr/libexec/PlistBuddy -c 'Print CFBundleIdentifier' "$APP_BUNDLE/Info.plist" 2>/dev/null || echo '?')"
VERSION="$(/usr/libexec/PlistBuddy -c 'Print CFBundleShortVersionString' "$APP_BUNDLE/Info.plist" 2>/dev/null || echo '?')"

echo
echo "==> Done (UNSIGNED — re-sign with your sideloader)"
echo "    File:      $OUT_IPA"
echo "    Bundle ID: $BUNDLE_ID"
echo "    Version:   $VERSION"
echo "    Size:      $(du -h "$OUT_IPA" | cut -f1)"
