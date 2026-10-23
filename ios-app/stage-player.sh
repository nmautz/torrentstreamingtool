#!/usr/bin/env bash
#
# stage-player.sh — put the STARTER copy of the dashboard player into www/player/.
#
# Run by Capacitor before every `cap copy` / `cap sync` (the `capacitor:copy:before`
# hook in package.json), so build-ipa.sh and a manual `npx cap sync ios` before an
# Xcode build both ship it. Copies static/index.html and static/vendor/ as they
# are at build time; the app serves them offline from LocalMediaServer
# (`start({bundledPlayer: "player"})`) when no snapshot downloaded from a server is
# complete. See docs/STREAMING.md § Offline entry point.
#
# The vendor files are the /api/player-manifest allowlist (_PLAYER_SNAPSHOT_FILES
# in main.py). Copying the whole vendor/ dir keeps the two from drifting as long as
# vendor/ holds nothing the page doesn't load.
#
# www/player/ is a build artifact and gitignored. It is wiped and rebuilt each run.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATIC="$SCRIPT_DIR/../static"
OUT="$SCRIPT_DIR/www/player"

if [[ ! -f "$STATIC/index.html" || ! -d "$STATIC/vendor" ]]; then
  echo "stage-player: $STATIC/index.html or vendor/ missing" >&2
  exit 1
fi

rm -rf "$OUT"
mkdir -p "$OUT/vendor"
cp "$STATIC/index.html" "$OUT/index.html"
cp "$STATIC/vendor/"* "$OUT/vendor/"

VER="$(sed -n 's/.*data-ui-version[^>]*>\([^<]*\)<.*/\1/p' "$OUT/index.html" | tail -1)"
echo "==> Staged starter player $VER into www/player/ ($(ls "$OUT/vendor" | wc -l | tr -d ' ') vendor files)"
