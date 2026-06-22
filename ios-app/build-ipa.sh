#!/usr/bin/env bash
#
# build-ipa.sh — produce an UNSIGNED StreamLink .ipa for sideloading.
#
# The output is intentionally unsigned: a re-signing sideloader (Sideloadly,
# AltStore, ESign, …) re-signs it with your free Apple ID on-device, so no Mac
# is needed for the weekly 7-day refresh. The .ipa will NOT install directly.
#
# Usage:
#   ./build-ipa.sh                 # copy www/, build, package
#   ./build-ipa.sh --no-copy       # skip `cap copy` (web assets already current)
#
# Requires: Xcode + the iOS device platform component, Node/npm (for cap copy).

set -euo pipefail

# Resolve repo-relative paths so the script works from any CWD.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$SCRIPT_DIR/ios/App"
DERIVED="$APP_DIR/build"
PRODUCTS="$DERIVED/Build/Products/Release-iphoneos"
IPA_NAME="StreamLink-unsigned.ipa"
OUT_IPA="$SCRIPT_DIR/$IPA_NAME"

DO_COPY=1
for arg in "$@"; do
  case "$arg" in
    --no-copy) DO_COPY=0 ;;
    -h|--help) sed -n '2,16p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

cd "$SCRIPT_DIR"

if [[ "$DO_COPY" -eq 1 ]]; then
  echo "==> Copying www/ into the iOS project (npx cap copy ios)"
  npx cap copy ios
else
  echo "==> Skipping cap copy (--no-copy)"
fi

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
