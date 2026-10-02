#!/usr/bin/env bash
#
# publish-ipa.sh — build the unsigned .ipa and publish it to the SideStore /
# AltStore source, so people install and update StreamLink from SideStore
# instead of building it.
#
# The source is a separate PUBLIC repo (default nmautz/streamlink-ios):
#   - a GitHub Release per version, tagged v<version>, holding StreamLink.ipa
#   - one source file per release CHANNEL at the repo root (20.1.0). A channel
#     is a server branch, and people add the source that matches the branch
#     their server follows, under Sources → +:
#       main   https://raw.githubusercontent.com/<repo>/main/apps.json
#       beta   https://raw.githubusercontent.com/<repo>/main/apps-beta.json
#       alpha  https://raw.githubusercontent.com/<repo>/main/apps-alpha.json
#     That is what keeps an app built from alpha away from a server on main.
#     The file names come from appchannel.py, which the server reads too.
#
# The version is the dashboard badge (static/index.html), which build-ipa.sh
# stamps into the app. SideStore compares it with the installed app to offer
# the update, so a version can be BUILT once only: bump the badge first.
#
# A build is published to the channel of the branch it was built on. It reaches
# a steadier channel by being PROMOTED: the same released .ipa is added to that
# channel's source, nothing is rebuilt. `promote.py` at the repo root does that
# together with moving the branch; `--promote` here is the app half of it.
#
# Usage:
#   ./publish-ipa.sh                  # full build, then publish to this branch's channel
#   ./publish-ipa.sh --skip-build     # publish the .ipa already built (must match the badge)
#   ./publish-ipa.sh --notes "text"   # release notes (default: the CHANGELOG heading)
#   ./publish-ipa.sh --channel alpha  # name the channel (needed off main/beta/alpha)
#   ./publish-ipa.sh --dry-run        # build and print the source file, publish nothing
#   ./publish-ipa.sh --promote --channel main --version 20.0.1
#                                     # add an already-released version to a channel
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
PROMOTE=0
NOTES=""
CHANNEL=""
VERSION=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-build) DO_BUILD=0 ;;
    --dry-run)    DRY=1 ;;
    --promote)    PROMOTE=1 ;;
    --notes)      NOTES="${2:?--notes needs text}"; shift ;;
    --channel)    CHANNEL="${2:?--channel needs main, beta or alpha}"; shift ;;
    --version)    VERSION="${2:?--version needs x.y.z}"; shift ;;
    -h|--help)    sed -n '2,37p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

# The channel is the branch the build came from. A feature branch has none, so
# it has to be named: guessing would put an experiment in front of main users.
if [[ -z "$CHANNEL" ]]; then
  CHANNEL="$(git -C "$ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
  FROM_BRANCH=1
fi
case "$CHANNEL" in
  main|beta|alpha) ;;
  *) if [[ "${FROM_BRANCH:-0}" -eq 1 ]]; then
       echo "ERROR: branch '$CHANNEL' is not a release channel. Pass --channel main|beta|alpha." >&2
     else
       echo "ERROR: --channel must be main, beta or alpha (got '$CHANNEL')." >&2
     fi
     exit 2 ;;
esac
SOURCE_FILE="$(cd "$ROOT" && python3 -c 'import sys, appchannel; print(appchannel.source_file(sys.argv[1]))' "$CHANNEL")"

if [[ -n "$VERSION" && "$PROMOTE" -eq 0 ]]; then
  echo "ERROR: --version only goes with --promote. A build's version is the badge." >&2
  exit 2
fi
if [[ -z "$VERSION" ]]; then
  VERSION="$(sed -n 's/.*data-ui-version[^>]*>\([0-9][0-9.]*\)<.*/\1/p' "$ROOT/static/index.html" | tail -1)"
fi
if [[ ! "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "ERROR: could not read the version from the static/index.html badge (got '$VERSION')." >&2
  exit 1
fi
TAG="v$VERSION"

if [[ "$DRY" -eq 0 || "$PROMOTE" -eq 1 ]]; then
  gh auth status >/dev/null 2>&1 || { echo "ERROR: gh is not logged in (gh auth login)." >&2; exit 1; }
  gh repo view "$REPO" >/dev/null 2>&1 || { echo "ERROR: $REPO does not exist or is not reachable." >&2; exit 1; }
  if gh release view "$TAG" -R "$REPO" >/dev/null 2>&1; then
    if [[ "$PROMOTE" -eq 0 ]]; then
      echo "ERROR: $TAG is already published on $REPO. Bump the badge in static/index.html first." >&2
      exit 1
    fi
  elif [[ "$PROMOTE" -eq 1 ]]; then
    echo "ERROR: $TAG has not been released on $REPO, so there is nothing to promote." >&2
    exit 1
  fi
fi

# Default release notes: the "### ..." line under this version in CHANGELOG.md.
if [[ -z "$NOTES" ]]; then
  NOTES="$(awk -v v="## [$VERSION]" 'index($0, v) == 1 {f = 1; next} f && /^### / {sub(/^### /, ""); print; exit} f && /^## / {exit}' "$ROOT/CHANGELOG.md")"
  NOTES="${NOTES:-StreamLink $VERSION}"
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

if [[ "$PROMOTE" -eq 1 ]]; then
  # The released file itself, not whatever is in the working tree: its
  # permissions are what the channel's source has to declare.
  echo "==> Fetching the released $TAG"
  gh release download "$TAG" -R "$REPO" -p "$ASSET" -D "$WORK/rel"
  IPA="$WORK/rel/$ASSET"
elif [[ "$DO_BUILD" -eq 1 ]]; then
  APP_VERSION="$VERSION" "$SCRIPT_DIR/build-ipa.sh"
fi
[[ -f "$IPA" ]] || { echo "ERROR: $IPA not found. Run without --skip-build." >&2; exit 1; }

# Read the facts SideStore checks straight out of the .ipa we are about to ship.
unzip -q "$IPA" -d "$WORK/ipa"
APP="$(ls -d "$WORK"/ipa/Payload/*.app | head -1)"
PLIST="$APP/Info.plist"
IPA_VERSION="$(/usr/libexec/PlistBuddy -c 'Print CFBundleShortVersionString' "$PLIST")"
IPA_BUILD="$(/usr/libexec/PlistBuddy -c 'Print CFBundleVersion' "$PLIST")"
BUNDLE_ID="$(/usr/libexec/PlistBuddy -c 'Print CFBundleIdentifier' "$PLIST")"
MIN_OS="$(/usr/libexec/PlistBuddy -c 'Print MinimumOSVersion' "$PLIST" 2>/dev/null || echo 15.0)"
if [[ "$IPA_VERSION" != "$VERSION" ]]; then
  echo "ERROR: the .ipa says $IPA_VERSION but $VERSION was expected. Rebuild (drop --skip-build)." >&2
  exit 1
fi
plutil -convert json -o "$WORK/info.json" "$PLIST"
codesign -d --entitlements :- "$APP" > "$WORK/ent.plist" 2>/dev/null || true
if [[ -s "$WORK/ent.plist" ]]; then plutil -convert json -o "$WORK/ent.json" "$WORK/ent.plist"; else echo '{}' > "$WORK/ent.json"; fi

SIZE="$(stat -f %z "$IPA")"
DATE="$(date +%Y-%m-%d)"
URL="https://github.com/$REPO/releases/download/$TAG/$ASSET"

# The source file = the template's metadata + every version already in this
# channel, newest first (SideStore offers versions[0]).
# SideStore refuses an install whose app uses permissions the source did not
# declare, so they are read from the app itself, never written by hand. They
# are replaced only when this version becomes the channel's newest: the
# declaration has to describe the app the source is offering.
build_source() {  # $1 = the channel's existing source file (may be missing), $2 = output
  python3 - "$TEMPLATE" "$1" "$2" "$REPO" "$BUNDLE_ID" "$VERSION" "$IPA_BUILD" "$DATE" "$NOTES" "$URL" "$SIZE" "$MIN_OS" "$WORK/info.json" "$WORK/ent.json" "$CHANNEL" <<'PY'
import json, os, sys
(tpl, old, out, repo, bid, ver, build, date, notes, url, size, min_os, info, ent, channel) = sys.argv[1:]
key = lambda v: tuple(int(n) if n.isdigit() else 0 for n in str(v.get("version", "")).split("."))
src = json.loads(open(tpl).read().replace("{repo}", repo))
# Each channel is its own source to SideStore, so someone who adds two of them
# sees two, clearly named, instead of one silently replacing the other.
if channel != "main":
    src["identifier"] += "." + channel
    src["name"] += " (%s)" % channel
    src["subtitle"] = "The StreamLink iOS app, %s channel. For a server on the %s branch." % (channel, channel)
app = next(a for a in src["apps"] if a["bundleIdentifier"] == bid)
prior, perms = [], None
if os.path.exists(old):
    for a in json.load(open(old)).get("apps", []):
        if a.get("bundleIdentifier") == bid:
            prior = [v for v in a.get("versions", []) if v.get("version") != ver]
            perms = a.get("appPermissions")
info = json.load(open(info))
entry = {
    "version": ver, "buildVersion": build, "date": date,
    "localizedDescription": notes, "downloadURL": url,
    "size": int(size), "minOSVersion": min_os,
}
app["versions"] = sorted([entry] + prior, key=key, reverse=True)
if app["versions"][0] is entry or not perms:
    perms = {
        "entitlements": sorted(json.load(open(ent)).keys()),
        "privacy": {k: v for k, v in sorted(info.items()) if k.startswith("NS") and k.endswith("UsageDescription")},
    }
app["appPermissions"] = perms
json.dump(src, open(out, "w"), indent=2, ensure_ascii=False)
open(out, "a").write("\n")
PY
}

if [[ "$DRY" -eq 1 && "$PROMOTE" -eq 0 ]]; then
  build_source /nonexistent "$WORK/$SOURCE_FILE"
  echo "==> Dry run: would publish $TAG to $REPO ($SIZE bytes), channel $CHANNEL, with this $SOURCE_FILE:"
  cat "$WORK/$SOURCE_FILE"
  exit 0
fi

if [[ "$PROMOTE" -eq 0 ]]; then
  echo "==> Uploading $TAG to $REPO"
  cp "$IPA" "$WORK/$ASSET"
  gh release create "$TAG" "$WORK/$ASSET" -R "$REPO" --title "StreamLink $VERSION" --notes "$NOTES"
fi

# Only after the upload landed does the source point at it.
gh repo clone "$REPO" "$WORK/repo" -- --quiet
if [[ "$PROMOTE" -eq 1 ]] && python3 - "$WORK/repo/$SOURCE_FILE" "$BUNDLE_ID" "$VERSION" <<'PY'
import json, os, sys
path, bid, ver = sys.argv[1:]
have = []
if os.path.exists(path):
    for a in json.load(open(path)).get("apps", []):
        if a.get("bundleIdentifier") == bid:
            have = [v.get("version") for v in a.get("versions", [])]
sys.exit(0 if ver in have else 1)
PY
then
  echo "==> $VERSION is already on the $CHANNEL channel. Nothing to do."
  exit 0
fi
build_source "$WORK/repo/$SOURCE_FILE" "$WORK/repo/$SOURCE_FILE"
if [[ "$DRY" -eq 1 ]]; then
  echo "==> Dry run: would add $VERSION to the $CHANNEL channel. $SOURCE_FILE would become:"
  cat "$WORK/repo/$SOURCE_FILE"
  exit 0
fi
echo "==> Updating $SOURCE_FILE"
[[ -f "$WORK/repo/icon.png" ]] || cp "$ICON" "$WORK/repo/icon.png"
git -C "$WORK/repo" add "$SOURCE_FILE" icon.png
if [[ "$PROMOTE" -eq 1 ]]; then MSG="StreamLink $VERSION → $CHANNEL"; else MSG="StreamLink $VERSION ($CHANNEL)"; fi
git -C "$WORK/repo" commit -q -m "$MSG"
git -C "$WORK/repo" push -q origin HEAD

echo
if [[ "$PROMOTE" -eq 1 ]]; then echo "==> Promoted StreamLink $VERSION to $CHANNEL"; else echo "==> Published StreamLink $VERSION to $CHANNEL"; fi
echo "    Release: https://github.com/$REPO/releases/tag/$TAG"
echo "    Source:  https://raw.githubusercontent.com/$REPO/main/$SOURCE_FILE"
