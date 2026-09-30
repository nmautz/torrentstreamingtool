#!/usr/bin/env bash
#
# publish-ipa.sh — build the unsigned .ipa and publish it to the SideStore /
# AltStore source, so people install and update StreamLink from SideStore
# instead of building it.
#
# The source is a separate PUBLIC repo (default nmautz/streamlink-ios):
#   - a GitHub Release per version, tagged v<version>, holding StreamLink.ipa
#   - apps.json at the repo root: the source SideStore reads. Users add
#       https://raw.githubusercontent.com/<repo>/main/apps.json
#     under Sources → +.
#
# The version is the dashboard badge (static/index.html), which build-ipa.sh
# stamps into the app. SideStore compares it with the installed app to offer
# the update, so a version can be published once only: bump the badge first.
#
# Usage:
#   ./publish-ipa.sh                  # full build, then publish
#   ./publish-ipa.sh --skip-build     # publish the .ipa already built (must match the badge)
#   ./publish-ipa.sh --notes "text"   # release notes (default: the CHANGELOG heading)
#   ./publish-ipa.sh --dry-run        # build and print the apps.json entry, publish nothing
#
# Env: SIDESTORE_REPO=owner/name to publish somewhere else.
# Requires: everything build-ipa.sh needs, plus gh (logged in) and python3.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO="${SIDESTORE_REPO:-nmautz/streamlink-ios}"
IPA="$SCRIPT_DIR/StreamLink-unsigned.ipa"
ASSET="StreamLink.ipa"
TEMPLATE="$SCRIPT_DIR/sidestore/source.json"
ICON="$SCRIPT_DIR/ios/App/App/Assets.xcassets/AppIcon.appiconset/AppIcon-512@2x.png"

DO_BUILD=1
DRY=0
NOTES=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-build) DO_BUILD=0 ;;
    --dry-run)    DRY=1 ;;
    --notes)      NOTES="${2:?--notes needs text}"; shift ;;
    -h|--help)    sed -n '2,26p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

VERSION="$(sed -n 's/.*data-ui-version[^>]*>\([0-9][0-9.]*\)<.*/\1/p' "$ROOT/static/index.html" | tail -1)"
if [[ ! "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "ERROR: could not read the version from the static/index.html badge (got '$VERSION')." >&2
  exit 1
fi
TAG="v$VERSION"

if [[ "$DRY" -eq 0 ]]; then
  gh auth status >/dev/null 2>&1 || { echo "ERROR: gh is not logged in (gh auth login)." >&2; exit 1; }
  gh repo view "$REPO" >/dev/null 2>&1 || { echo "ERROR: $REPO does not exist or is not reachable." >&2; exit 1; }
  if gh release view "$TAG" -R "$REPO" >/dev/null 2>&1; then
    echo "ERROR: $TAG is already published on $REPO. Bump the badge in static/index.html first." >&2
    exit 1
  fi
fi

# Default release notes: the "### ..." line under this version in CHANGELOG.md.
if [[ -z "$NOTES" ]]; then
  NOTES="$(awk -v v="## [$VERSION]" 'index($0, v) == 1 {f = 1; next} f && /^### / {sub(/^### /, ""); print; exit} f && /^## / {exit}' "$ROOT/CHANGELOG.md")"
  NOTES="${NOTES:-StreamLink $VERSION}"
fi

if [[ "$DO_BUILD" -eq 1 ]]; then
  APP_VERSION="$VERSION" "$SCRIPT_DIR/build-ipa.sh"
fi
[[ -f "$IPA" ]] || { echo "ERROR: $IPA not found. Run without --skip-build." >&2; exit 1; }

# Read the facts SideStore checks straight out of the .ipa we are about to ship.
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
unzip -q "$IPA" -d "$WORK/ipa"
APP="$(ls -d "$WORK"/ipa/Payload/*.app | head -1)"
PLIST="$APP/Info.plist"
IPA_VERSION="$(/usr/libexec/PlistBuddy -c 'Print CFBundleShortVersionString' "$PLIST")"
IPA_BUILD="$(/usr/libexec/PlistBuddy -c 'Print CFBundleVersion' "$PLIST")"
BUNDLE_ID="$(/usr/libexec/PlistBuddy -c 'Print CFBundleIdentifier' "$PLIST")"
MIN_OS="$(/usr/libexec/PlistBuddy -c 'Print MinimumOSVersion' "$PLIST" 2>/dev/null || echo 15.0)"
if [[ "$IPA_VERSION" != "$VERSION" ]]; then
  echo "ERROR: the .ipa says $IPA_VERSION but the badge says $VERSION. Rebuild (drop --skip-build)." >&2
  exit 1
fi
plutil -convert json -o "$WORK/info.json" "$PLIST"
codesign -d --entitlements :- "$APP" > "$WORK/ent.plist" 2>/dev/null || true
if [[ -s "$WORK/ent.plist" ]]; then plutil -convert json -o "$WORK/ent.json" "$WORK/ent.plist"; else echo '{}' > "$WORK/ent.json"; fi

SIZE="$(stat -f %z "$IPA")"
DATE="$(date +%Y-%m-%d)"
URL="https://github.com/$REPO/releases/download/$TAG/$ASSET"

# apps.json = the template's metadata + every version already published, newest first.
# SideStore refuses an install whose app uses permissions the source did not
# declare, so they are read from the built app, never written by hand.
build_source() {  # $1 = existing apps.json (may be missing), $2 = output
  python3 - "$TEMPLATE" "$1" "$2" "$REPO" "$BUNDLE_ID" "$VERSION" "$IPA_BUILD" "$DATE" "$NOTES" "$URL" "$SIZE" "$MIN_OS" "$WORK/info.json" "$WORK/ent.json" <<'PY'
import json, os, sys
(tpl, old, out, repo, bid, ver, build, date, notes, url, size, min_os, info, ent) = sys.argv[1:]
src = json.loads(open(tpl).read().replace("{repo}", repo))
app = next(a for a in src["apps"] if a["bundleIdentifier"] == bid)
prior = []
if os.path.exists(old):
    for a in json.load(open(old)).get("apps", []):
        if a.get("bundleIdentifier") == bid:
            prior = [v for v in a.get("versions", []) if v.get("version") != ver]
info = json.load(open(info))
app["appPermissions"] = {
    "entitlements": sorted(json.load(open(ent)).keys()),
    "privacy": {k: v for k, v in sorted(info.items()) if k.startswith("NS") and k.endswith("UsageDescription")},
}
app["versions"] = [{
    "version": ver, "buildVersion": build, "date": date,
    "localizedDescription": notes, "downloadURL": url,
    "size": int(size), "minOSVersion": min_os,
}] + prior
json.dump(src, open(out, "w"), indent=2, ensure_ascii=False)
open(out, "a").write("\n")
PY
}

if [[ "$DRY" -eq 1 ]]; then
  build_source /nonexistent "$WORK/apps.json"
  echo "==> Dry run: would publish $TAG to $REPO ($SIZE bytes) with this apps.json:"
  cat "$WORK/apps.json"
  exit 0
fi

echo "==> Uploading $TAG to $REPO"
cp "$IPA" "$WORK/$ASSET"
gh release create "$TAG" "$WORK/$ASSET" -R "$REPO" --title "StreamLink $VERSION" --notes "$NOTES"

# Only after the upload landed does the source point at it.
echo "==> Updating apps.json"
gh repo clone "$REPO" "$WORK/repo" -- --quiet
build_source "$WORK/repo/apps.json" "$WORK/repo/apps.json"
[[ -f "$WORK/repo/icon.png" ]] || cp "$ICON" "$WORK/repo/icon.png"
git -C "$WORK/repo" add apps.json icon.png
git -C "$WORK/repo" commit -q -m "StreamLink $VERSION"
git -C "$WORK/repo" push -q origin HEAD

echo
echo "==> Published StreamLink $VERSION"
echo "    Release: https://github.com/$REPO/releases/tag/$TAG"
echo "    Source:  https://raw.githubusercontent.com/$REPO/main/apps.json"
