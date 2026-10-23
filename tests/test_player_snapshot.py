"""The offline player's two copies must carry the same files. Plain python, no deps:

    python tests/test_player_snapshot.py      (or `make test`)

The app gets the dashboard player two ways (docs/STREAMING.md § Offline entry
point): downloaded from the server against /api/player-manifest's allowlist
(`_PLAYER_SNAPSHOT_FILES` in main.py), and as the starter copy built into the
app by ios-app/stage-player.sh, which copies static/index.html and the WHOLE of
static/vendor/. Those agree only while vendor/ holds exactly the allowlist.

  1. Every allowlisted file exists in static/.
  2. static/vendor/ holds nothing the allowlist leaves out (it would ship in the
     starter copy and never reach a downloaded one).
  3. The three player slot names agree: static/index.html (_APP_PLAYER_SLOTS),
     the shell (PLAYER_SLOTS) and BundleDownloader.swift (playerKeys).
"""
import ast, os, re, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
fails = []
passed = 0


def check(name, ok, detail=""):
    global passed
    if ok:
        passed += 1
    else:
        fails.append(name + (": " + detail if detail else ""))


def read(*p):
    with open(os.path.join(ROOT, *p), encoding="utf-8") as fh:
        return fh.read()


src = read("main.py")
m = re.search(r"^_PLAYER_SNAPSHOT_FILES = (\[.*?\])", src, re.S | re.M)
check("main.py defines _PLAYER_SNAPSHOT_FILES", bool(m))
allow = ast.literal_eval(m.group(1)) if m else []

missing = [f for f in allow if not os.path.isfile(os.path.join(ROOT, "static", f))]
check("every allowlisted file is in static/", not missing, ", ".join(missing))

vendor = sorted("vendor/" + f for f in os.listdir(os.path.join(ROOT, "static", "vendor")))
extra = [f for f in vendor if f not in allow]
check("static/vendor/ holds only allowlisted files", not extra,
      ", ".join(extra) + " (add to _PLAYER_SNAPSHOT_FILES, or move out of vendor/)")
check("index.html is allowlisted", "index.html" in allow)


def names(pattern, text):
    mm = re.search(pattern, text)
    return sorted(re.findall(r'"(__player[^"]*)"', mm.group(1))) if mm else None


dash = names(r"const _APP_PLAYER_SLOTS = \[([^\]]*)\]", read("static", "index.html"))
shell = names(r"const PLAYER_SLOTS = \[([^\]]*)\]", read("ios-app", "www", "index.html"))
swift = names(r"static let playerKeys: Set<String> = \[([^\]]*)\]",
              read("ios-app", "ios", "App", "App", "BundleDownloader.swift"))
check("dashboard, shell and native name the same player slots",
      dash is not None and dash == shell == swift and len(dash) == 2,
      "dashboard %s, shell %s, native %s" % (dash, shell, swift))

print("player_snapshot: %d passed, %d failed" % (passed, len(fails)))
for f in fails:
    print("  FAIL " + f)
sys.exit(1 if fails else 0)
