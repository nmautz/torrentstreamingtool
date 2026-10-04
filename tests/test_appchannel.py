"""Unit tests for `appchannel.py`. Run with plain python, no deps:

    python tests/test_appchannel.py      (or `make test`)

The module decides which iOS app a server may point people at. Every case
leans on the one mistake it exists to prevent: telling someone to install an
app that is newer than the server it will talk to.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import appchannel as ac          # noqa: E402

_PASS = 0
_FAIL = []


def eq(name, got, want):
    global _PASS
    if got == want:
        _PASS += 1
    else:
        _FAIL.append("%s\n     got:  %r\n     want: %r" % (name, got, want))


def V(*vs):
    return [{"version": v} for v in vs]


# ── parse_version ────────────────────────────────────────────────────────────
eq("x.y.z", ac.parse_version("20.1.0"), (20, 1, 0))
eq("numeric, not lexical", ac.parse_version("19.14.0") > ac.parse_version("19.9.2"), True)
eq("a dev build is not a version", ac.parse_version("1.0"), None)
eq("empty", ac.parse_version(""), None)
eq("None", ac.parse_version(None), None)
eq("a suffix is not a version", ac.parse_version("20.1.0-beta"), None)

# ── channel_for ──────────────────────────────────────────────────────────────
eq("alpha checkout", ac.channel_for("alpha"), "alpha")
eq("main checkout", ac.channel_for("main"), "main")
eq("detached HEAD uses the updater's branch", ac.channel_for("HEAD", "beta"), "beta")
eq("a feature branch uses the updater's branch", ac.channel_for("fix/thing", "alpha"), "alpha")
eq("the checkout wins over the config", ac.channel_for("main", "alpha"), "main")
eq("nothing known is main", ac.channel_for("", ""), "main")
eq("a feature branch tracking itself is main", ac.channel_for("fix/thing", "fix/thing"), "main")

# ── source files ─────────────────────────────────────────────────────────────
eq("main keeps the URL people already added", ac.source_file("main"), "apps.json")
eq("alpha file", ac.source_file("alpha"), "apps-alpha.json")
eq("url", ac.source_url("beta", "o/r"),
   "https://raw.githubusercontent.com/o/r/main/apps-beta.json")

# ── fallback: only ever towards steadier ─────────────────────────────────────
eq("alpha may borrow beta's, then main's", ac.fallback_chain("alpha"), ["alpha", "beta", "main"])
eq("beta may borrow main's", ac.fallback_chain("beta"), ["beta", "main"])
eq("main borrows nothing", ac.fallback_chain("main"), ["main"])
eq("unknown is main", ac.fallback_chain("nope"), ["main"])
eq("urls follow the chain", [c for c, _ in ac.source_urls("alpha")], ["alpha", "beta", "main"])

# ── versions_of ──────────────────────────────────────────────────────────────
SRC = {"apps": [{"bundleIdentifier": "other", "versions": V("9.9.9")},
                {"bundleIdentifier": ac.BUNDLE_ID, "versions": V("20.0.1", "19.14.0") + ["junk"]}]}
eq("the app's entries, junk dropped", ac.versions_of(SRC), V("20.0.1", "19.14.0"))
eq("no such app", ac.versions_of({"apps": []}), [])
eq("not a source", ac.versions_of(None), [])

# ── pick: never newer than the server ────────────────────────────────────────
vs = V("20.0.1", "19.14.0", "19.9.2", "19.4.1")
eq("no ceiling is the newest", ac.pick(vs)["version"], "20.0.1")
eq("a server on 19.14.0 is not sent to 20.0.1", ac.pick(vs, "19.14.0")["version"], "19.14.0")
eq("between two app versions takes the older", ac.pick(vs, "19.13.1")["version"], "19.9.2")
eq("the badge moves for host-only changes", ac.pick(vs, "20.0.3")["version"], "20.0.1")
eq("a server older than every app gets none", ac.pick(vs, "19.0.0"), None)
eq("order in the file does not matter", ac.pick(V("19.4.1", "20.0.1", "19.9.2"))["version"], "20.0.1")
eq("an unparseable ceiling is no ceiling", ac.pick(vs, "dev")["version"], "20.0.1")
eq("unparseable entries are skipped", ac.pick(V("1.0", "19.4.1"))["version"], "19.4.1")
eq("nothing published", ac.pick([]), None)

# ── app_move ─────────────────────────────────────────────────────────────────
rel = V("20.1.0", "20.0.1", "19.14.0", "19.9.2")
mv = lambda target, have=(): (lambda a, e: (a, e and e["version"]))(*ac.app_move(rel, target, have))
eq("promoting 20.0.3 gains app 20.0.1", mv("20.0.3", V("19.4.1")), ("gain", "20.0.1"))
eq("an empty channel gains it", mv("20.1.0"), ("gain", "20.1.0"))
eq("already there", mv("20.0.3", V("20.0.1", "19.14.0")), ("keep", "20.0.1"))
eq("a source ahead of the server is cut back", mv("19.13.1", V("20.0.1", "19.14.0", "19.9.2")), ("cut", "19.9.2"))
eq("cut back to an app it does not hold yet", mv("19.14.0", V("20.0.1")), ("cut", "19.14.0"))
eq("cut to nothing when no app is old enough", mv("19.0.0", V("20.0.1")), ("cut", None))
eq("no app old enough, nothing offered", mv("19.0.0"), ("none", None))
eq("an older app in the source is not a reason to cut", mv("20.0.3", V("19.9.2")), ("gain", "20.0.1"))

# ── unverified_claims ────────────────────────────────────────────────────────
LOG = """# Changelog

## [20.1.0] — 2026-10-02
### Channels
- New thing. Not yet checked on a phone.

## [20.0.0] — 2026-10-01
### Tabs
- **New: tabs.** Works.
- Checked in a desktop browser against a simulated app. The app builds.
  Not yet checked on a phone.
* Season packs have unit tests only.

## [19.14.0] — 2026-10-01
- Checked on an iPhone 16. Not yet checked: the Bonjour path.

## [19.13.1] — 2026-10-01
- **Fixed (not yet confirmed on a phone): the stutter.**

## [19.4.1] — 2026-09-28
- Untested on Linux.
"""
got = ac.unverified_claims(LOG, "19.13.1", "20.0.0")
eq("only versions after the channel's and up to the target",
   sorted({v for v, _ in got}), ["19.14.0", "20.0.0"])
eq("a wrapped bullet is read whole",
   [t for v, t in got if v == "20.0.0"][0],
   "Checked in a desktop browser against a simulated app. The app builds. Not yet checked on a phone.")
eq("every admission in range is found", len(got), 3)
eq("the target's own entry counts", [v for v, _ in ac.unverified_claims(LOG, "20.0.0", "20.1.0")], ["20.1.0"])
eq("the channel's current version does not", ac.unverified_claims(LOG, "19.4.1", "19.13.0"), [])
eq("a parenthesised admission", len(ac.unverified_claims(LOG, "19.4.1", "19.13.1")), 1)
eq("a clean range", ac.unverified_claims("## [1.0.1]\n- Fixed. Checked on a phone.\n", "1.0.0", "1.0.1"), [])
eq("no changelog", ac.unverified_claims("", "1.0.0", "2.0.0"), [])

# ── changelog_entries ────────────────────────────────────────────────────────
LOG2 = """# Changelog

## [20.1.0] — 2026-10-02
### Channels
- **New: one source per channel.** Words.
- **Changed: the notice.** Words.

## [20.0.1] — 2026-10-01
### A resumed download showed 0 B
- **Fixed: a resumed download showed "0 B".** It
  wrapped.
- **Fixed (not yet confirmed on a phone): another.**

## [20.0.0] — 2026-10-01
### Tabs
- **New: tabs.**
- **Removed: the menu.**

## [19.14.0] — 2026-09-30
### Discovery
- **New: discovery.**
"""
es = ac.changelog_entries(LOG2, "19.14.0", "20.1.0")
eq("entries newest first, range respected", [e["version"] for e in es], ["20.1.0", "20.0.1", "20.0.0"])
eq("date and title", (es[0]["date"], es[0]["title"]), ("2026-10-02", "Channels"))
eq("bump kinds", [e["kind"] for e in es], ["y", "z", "x"])
eq("bullets are counted by kind", (es[0]["new"], es[0]["changed"], es[1]["fixed"], es[2]["removed"]), (1, 1, 2, 1))
eq("a fix is quoted without the markup", es[1]["fixes"][0], 'Fixed: a resumed download showed "0 B". It')
eq("upto cuts the top", [e["version"] for e in ac.changelog_entries(LOG2, "19.14.0", "20.0.1")], ["20.0.1", "20.0.0"])
eq("the oldest entry has no bump to measure", ac.changelog_entries(LOG2, "19.0.0", "19.14.0")[0]["kind"], "?")
eq("bump_kind", (ac.bump_kind("19.14.0", "20.0.0"), ac.bump_kind("20.0.0", "20.1.0"), ac.bump_kind("20.1.0", "20.1.1")), ("x", "y", "z"))

# ── log_health ───────────────────────────────────────────────────────────────
L = """2026-09-30 07:59:00  ERROR   [streamlink] before any banner: belongs to no build
2026-09-30 08:00:00  INFO    [streamlink] StreamLink v20.0.0 starting — branch=alpha commit=aaa
2026-09-30 08:10:00  ERROR   [streamlink.hls] job 9d788318fc7127a8 ABORT: no video stream in /lib/A.mkv
2026-09-30 09:00:00  ERROR   [streamlink.hls] job 1122334455667788 ABORT: no video stream in /lib/B.mkv
2026-09-30 10:00:00  INFO    [streamlink] tick
2026-10-01 08:00:00  INFO    [streamlink] StreamLink v20.0.1 starting — branch=alpha commit=bbb
2026-10-01 08:30:00  ERROR   [streamlink.hls] job 99aabbccddeeff00 ABORT: no video stream in /lib/C.mkv
2026-10-01 09:00:00  ERROR   [streamlink] queue row 14 has no stage
Traceback (most recent call last):
  File "main.py", line 1, in x
KeyError: 'stage'
2026-10-01 12:00:00  WARNING [streamlink] slow
2026-10-02 08:00:00  INFO    [streamlink] StreamLink v20.0.1 starting — branch=alpha commit=bbb
2026-10-02 09:00:00  INFO    [streamlink] tick
""".splitlines()
h = ac.log_health(L)
eq("versions newest first", [r["version"] for r in h], ["20.0.1", "20.0.0"])
eq("runs are counted per version", [r["runs"] for r in h], [2, 1])
eq("time is summed per run, not across the gap between them", h[0]["seconds"], 4 * 3600 + 3600)
eq("a run's time ends at its last line", h[1]["seconds"], 2 * 3600)
eq("errors per version", [r["errors"] for r in h], [2, 2])
eq("tracebacks per version", [r["tracebacks"] for r in h], [1, 0])
eq("the same failure on different files is one signature", len(h[1]["signatures"]), 1)
eq("its count", list(h[1]["signatures"].values()), [2])
eq("branch", h[0]["branch"], "alpha")
eq("first and last seen", (h[0]["first"], h[0]["last"]), ("2026-10-01 08:00:00", "2026-10-02 09:00:00"))
eq("only what the older build did not also do is new",
   ac.new_signatures(h, "20.0.1"), [("[streamlink] queue row # has no stage", 1)])
eq("the oldest build in the logs owns everything it logged", len(ac.new_signatures(h, "20.0.0")), 1)
eq("a version that never ran", ac.new_signatures(h, "20.1.0"), [])
eq("no log", ac.log_health([]), [])
eq("exposure: as itself, and as itself or newer", ac.exposure(h, "20.0.0"), (2 * 3600, 7 * 3600))
eq("the newest build has only its own time", ac.exposure(h, "20.0.1"), (5 * 3600, 5 * 3600))
eq("a build that was never the backend still ran inside later ones", ac.exposure(h, "20.0.0"[:-1] + "1")[1], 5 * 3600)
eq("a build newer than anything in the logs", ac.exposure(h, "21.0.0"), (0, 0))
eq("a file name with spaces is not part of the signature",
   ac._signature("streamlink", "[analyzer] [no_skip_points] Hacks.2021.S01E02.1080p.x265-RARBG.mp4: Fingerprinting done"),
   ac._signature("streamlink", "[analyzer] [no_skip_points] Widows Bay S01E09 We Hope.mkv: Fingerprinting done"))
eq("nor one with brackets in it",
   ac._signature("streamlink", "[analyzer] [no_skip_points] Hacks.S01E02.x265-RARBG[eztv.re].mp4: Fingerprinting done"),
   "[streamlink] [analyzer] [no_skip_points] <file>: Fingerprinting done")
eq("what remains still says what failed",
   ac._signature("streamlink", "[analyzer] [no_skip_points] Widows Bay S01E09.mkv: Fingerprinting done"),
   "[streamlink] [analyzer] [no_skip_points] <file>: Fingerprinting done")

print("appchannel: %d passed, %d failed" % (_PASS, len(_FAIL)))
for f in _FAIL:
    print("  FAIL " + f)
sys.exit(1 if _FAIL else 0)
