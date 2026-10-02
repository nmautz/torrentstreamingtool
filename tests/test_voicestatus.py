"""Unit tests for `voicestatus.py`. Run with plain python, no deps:

    python tests/test_voicestatus.py      (or `make test`)

The module answers a spoken question about the library. The cases lean on the
two ways that answer can be wrong: naming the wrong title for what was said,
and a sentence that claims more than the facts carry ("100% downloaded" for a
torrent still running, a time left nobody estimated).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import voicestatus as vs          # noqa: E402

_PASS = 0
_FAIL = []


def eq(name, got, want):
    global _PASS
    if got == want:
        _PASS += 1
    else:
        _FAIL.append("%s\n     got:  %r\n     want: %r" % (name, got, want))


def ok(name, cond):
    eq(name, bool(cond), True)


NAMES = ["Star Wars (1977)", "Star Wars Rebels", "The Empire Strikes Back (1980)",
         "Hunter x Hunter", "Andor", "The Office", "Breaking Bad", "War of the Worlds (2005)"]


def top(q):
    r = vs.rank(q, NAMES)
    return NAMES[r[0][0]] if r else None


# ── which title ───────────────────────────────────────────────────────────────
eq("year is not spoken", top("star wars"), "Star Wars (1977)")
eq("exact beats longer name", vs.rank("star wars", NAMES)[0][1], 1.0)
ok("longer name still offered", any(NAMES[i] == "Star Wars Rebels" for i, _ in vs.rank("star wars", NAMES)))
eq("year said picks the year", top("star wars 1977"), "Star Wars (1977)")
eq("article added", top("the star wars"), "Star Wars (1977)")
eq("article dropped", top("office"), "The Office")
eq("dropped x", top("hunter hunter"), "Hunter x Hunter")
eq("dictation slip", top("andoor"), "Andor")
eq("case and punctuation", top("BREAKING-BAD!"), "Breaking Bad")
eq("partial name", top("empire strikes back"), "The Empire Strikes Back (1980)")
eq("one shared word is not a match", top("star trek"), None)
eq("unknown title", top("seinfeld"), None)
eq("empty query", vs.rank("", NAMES), [])
ok("'wars' alone does not outrank the film it names",
   top("wars") in ("Star Wars (1977)", "Star Wars Rebels", "War of the Worlds (2005)"))
eq("ties keep caller order", [i for i, _ in vs.rank("x", ["X", "x"])], [0, 1])

# ── durations ─────────────────────────────────────────────────────────────────
eq("seconds", vs.span(20), "less than a minute")
eq("one minute", vs.span(70), "1 minute")
eq("minutes", vs.span(14 * 60 + 10), "14 minutes")
eq("hour exact", vs.span(3600), "1 hour")
eq("hour and minutes round to five", vs.span(3600 + 22 * 60), "1 hour 20 minutes")
eq("hours", vs.span(3 * 3600 + 58 * 60), "4 hours")
eq("days", vs.span(3 * 86400), "3 days")

# ── one title ─────────────────────────────────────────────────────────────────
BASE = {"name": "Star Wars (1977)", "items": 1, "downloading": 0, "errors": 0,
        "prep": True, "files": 1, "prep_ready": 0, "prep_busy": 0, "prep_eta_secs": None}


def d(**kw):
    return vs.describe(dict(BASE, **kw))


eq("downloading with eta",
   d(downloading=1, done_bytes=62, total_bytes=100, eta_secs=840),
   "Not yet. Star Wars (1977) is 62% downloaded, about 14 minutes left.")
eq("downloading, eta unknown",
   d(downloading=1, done_bytes=62, total_bytes=100, eta_secs=-1),
   "Not yet. Star Wars (1977) is 62% downloaded.")
eq("never says 100% while downloading",
   d(downloading=1, done_bytes=999, total_bytes=1000, eta_secs=-1),
   "Not yet. Star Wars (1977) is 99% downloaded.")
eq("no size yet means no percentage",
   d(downloading=1, done_bytes=0, total_bytes=0, eta_secs=-1),
   "Not yet. Star Wars (1977) is still downloading.")
eq("finding peers",
   d(downloading=1, done_bytes=0, total_bytes=100, finding_peers=True),
   "Not yet. Star Wars (1977) hasn't started downloading; it's still finding peers.")
eq("idle window beats eta",
   d(downloading=1, done_bytes=10, total_bytes=100, eta_secs=600, paused=True),
   "Not yet. Star Wars (1977) is 10% downloaded, and it's waiting for the idle download window.")
eq("stalled",
   d(downloading=1, done_bytes=10, total_bytes=100, eta_secs=600, stalled=True),
   "Not yet. Star Wars (1977) is 10% downloaded, but it hasn't made progress in a while.")
eq("several downloads of one show",
   d(name="Andor", items=12, downloading=3, done_bytes=1, total_bytes=4, eta_secs=7200),
   "Not yet. 3 of 12 downloads for Andor are still going, 25% done, about 2 hours left.")
eq("failed, with reason",
   d(errors=1, error="No working release found."),
   "Star Wars (1977) failed to download: No working release found.")
eq("failed, no reason", d(errors=1, error=""), "Star Wars (1977) failed to download.")
eq("done, not prepped",
   d(), "Yes, Star Wars (1977) has finished downloading. It hasn't been prepped yet.")
eq("done, prepping with eta",
   d(prep_busy=1, prep_eta_secs=300),
   "Yes, Star Wars (1977) has finished downloading. It's being prepped now, about 5 minutes to go.")
eq("done, prepping, no estimate yet",
   d(prep_busy=1),
   "Yes, Star Wars (1977) has finished downloading. It's being prepped now.")
eq("done and prepped",
   d(prep_ready=1),
   "Yes, Star Wars (1977) has finished downloading. It's prepped and ready to stream.")
eq("prep unsupported says nothing about prep",
   d(prep=False), "Yes, Star Wars (1977) has finished downloading.")
eq("show, some prepped",
   d(name="Andor", items=1, files=12, prep_ready=5, prep_busy=1, prep_eta_secs=3600),
   "Yes, Andor has finished downloading. 5 of 12 episodes are prepped, about 1 hour to go.")
eq("show, prep idle",
   d(name="Andor", files=12, prep_ready=5),
   "Yes, Andor has finished downloading. 5 of 12 episodes are prepped.")
eq("some failed, rest done",
   d(name="Andor", items=12, errors=2, files=10, prep_ready=10),
   "Yes, Andor has finished downloading. 2 downloads failed. It's prepped and ready to stream.")

# ── everything ────────────────────────────────────────────────────────────────
eq("nothing", vs.overview([dict(BASE)]), "Nothing is downloading right now.")
eq("one",
   vs.overview([dict(BASE, downloading=1, done_bytes=1, total_bytes=2, eta_secs=120)]),
   "One thing is downloading: Star Wars (1977) at 50%, about 2 minutes left.")
ROWS = [dict(BASE, name="T%d" % i, downloading=1, done_bytes=i, total_bytes=10, eta_secs=-1)
        for i in range(1, 7)]
eq("many are capped",
   vs.overview(ROWS),
   "6 things are downloading: T1 at 10%; T2 at 20%; T3 at 30%; T4 at 40%; and 2 more.")
eq("finding peers in overview",
   vs.overview([dict(BASE, downloading=1, total_bytes=0, finding_peers=True)]),
   "One thing is downloading: Star Wars (1977), still finding peers.")
eq("unmeasured in overview",
   vs.overview([dict(BASE, downloading=1, total_bytes=0)]),
   "One thing is downloading: Star Wars (1977).")

# ── the full report: Siri answers about ANY title out of this one sentence ────
DONE = [dict(BASE, name="SpongeBob SquarePants", files=200, prep_ready=200),
        dict(BASE, name="Andor", files=12, prep_ready=5, prep_busy=1, prep_eta_secs=3600),
        dict(BASE, name="Alien (1979)"),
        dict(BASE, name="Heat (1995)", prep_busy=1),
        dict(BASE, name="Dune (2021)", errors=1),
        dict(BASE, name="Plain", prep=False)]
eq("finished titles carry their prep state",
   vs.overview([], DONE),
   "Nothing is downloading right now. Most recently finished downloading: "
   "SpongeBob SquarePants, prepped and ready to stream; "
   "Andor, 5 of 12 episodes prepped, about 1 hour to go; "
   "Alien (1979), not prepped yet; Heat (1995), being prepped now; "
   "Dune (2021) failed to download; Plain.")
eq("downloading first, then finished",
   vs.overview([dict(BASE, downloading=1, done_bytes=1, total_bytes=2, eta_secs=120)], DONE[:1]),
   "One thing is downloading: Star Wars (1977) at 50%, about 2 minutes left. "
   "Most recently finished downloading: SpongeBob SquarePants, prepped and ready to stream.")
eq("a title still downloading is never listed as finished",
   vs.overview([], [dict(BASE, downloading=1, total_bytes=10)]),
   "Nothing is downloading right now.")
ok("finished list is capped",
   vs.overview([], [dict(BASE, name="N%d" % i) for i in range(20)]).count(";") == vs.OVERVIEW_DONE_MAX - 1)

if _FAIL:
    print("FAILED %d (passed %d)" % (len(_FAIL), _PASS))
    for f in _FAIL:
        print("  - " + f)
    sys.exit(1)
print("voicestatus: %d passed" % _PASS)
