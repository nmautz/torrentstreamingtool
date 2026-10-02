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

# ── promotable ───────────────────────────────────────────────────────────────
alpha = V("20.1.0", "20.0.1", "19.14.0")
eq("promoting 20.0.3 moves app 20.0.1", ac.promotable(alpha, "20.0.3", V("19.4.1"))["version"], "20.0.1")
eq("promoting to an empty channel", ac.promotable(alpha, "20.1.0")["version"], "20.1.0")
eq("already there", ac.promotable(alpha, "20.0.3", V("20.0.1")), None)
eq("the channel is never moved backwards", ac.promotable(alpha, "19.14.0", V("20.0.1")), None)
eq("no app old enough", ac.promotable(alpha, "19.0.0"), None)

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

print("appchannel: %d passed, %d failed" % (_PASS, len(_FAIL)))
for f in _FAIL:
    print("  FAIL " + f)
sys.exit(1 if _FAIL else 0)
