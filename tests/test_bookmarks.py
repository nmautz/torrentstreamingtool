"""Unit tests for `bookmarks.py`. Run with plain python, no deps:

    python tests/test_bookmarks.py      (or `make test`)

The module decides when a bookmarked title counts as "something new is out".
The cases lean on the ways that notification could lie: lighting up for a film
that was already out when it was bookmarked, lighting up because TMDb went
quiet, or never lighting up because the refresh missed the moment.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bookmarks as bm          # noqa: E402

_PASS = 0
_FAIL = []


def eq(name, got, want):
    global _PASS
    if got == want:
        _PASS += 1
    else:
        _FAIL.append("%s\n     got:  %r\n     want: %r" % (name, got, want))


T = "2026-09-28"

# ── normalize ────────────────────────────────────────────────────────────────
eq("a candidate is kept",
   bm.normalize({"id": 42, "kind": "movie", "title": " Dune ", "year": "2026",
                 "poster_path": "/p.jpg", "date": "2026-09-01", "overview": "x"}),
   {"kind": "movie", "id": 42, "title": "Dune", "year": "2026",
    "poster_path": "/p.jpg", "date": "2026-09-01"})
eq("bad kind", bm.normalize({"id": 1, "kind": "person", "title": "x"}), None)
eq("no id", bm.normalize({"kind": "tv", "title": "x"}), None)
eq("junk id", bm.normalize({"id": "abc", "kind": "tv", "title": "x"}), None)
eq("no title", bm.normalize({"id": 1, "kind": "tv", "title": "  "}), None)
eq("not a dict", bm.normalize(None), None)
eq("a poster that isn't a TMDb path is dropped",
   bm.normalize({"id": 1, "kind": "tv", "title": "x", "poster_path": "http://evil"})["poster_path"], "")
eq("a malformed date is dropped",
   bm.normalize({"id": 1, "kind": "tv", "title": "x", "date": "soon"})["date"], "")

# ── movie_status ─────────────────────────────────────────────────────────────
eq("in theaters with a digital date: count down to it",
   bm.movie_status({"theatrical_only": True, "digital_date": "2026-10-20"}, "2026-09-05", T),
   {"state": "theaters", "date": "2026-10-20", "what": "digital"})
eq("in theaters, digital date unknown",
   bm.movie_status({"theatrical_only": True, "digital_date": ""}, "2026-09-05", T),
   {"state": "theaters", "date": "", "what": "digital"})
eq("digital date passed: out",
   bm.movie_status({"theatrical_only": False, "digital_date": "2026-09-01"}, "2026-07-05", T)["state"], "out")
eq("digital date is today: out",
   bm.movie_status({"digital_date": T}, "2026-07-05", T)["state"], "out")
eq("opened abroad, US digital date ahead: still waiting (theatrical_only False)",
   bm.movie_status({"theatrical_only": False, "digital_date": "2026-11-01"}, "2026-09-01", T),
   {"state": "theaters", "date": "2026-11-01", "what": "digital"})
eq("not in theaters yet, digital date known: count down to digital",
   bm.movie_status({"digital_date": "2027-02-01"}, "2026-12-18", T),
   {"state": "upcoming", "date": "2027-02-01", "what": "digital"})
eq("not released at all, no home date: count down to release",
   bm.movie_status({}, "2026-12-18", T),
   {"state": "upcoming", "date": "2026-12-18", "what": "release"})
eq("an old film TMDb has no home date for is out",
   bm.movie_status({"theatrical_only": False, "theatrical_date": "2001-05-01", "digital_date": ""},
                   "2001-05-01", T)["state"], "out")
eq("no data at all is out (nothing to wait for)", bm.movie_status({}, "", T)["state"], "out")

# ── tv_status ────────────────────────────────────────────────────────────────
eq("premieres later",
   bm.tv_status({"first_air_date": "2026-10-10"}, T),
   {"state": "upcoming", "date": "2026-10-10", "what": "premiere"})
eq("in production, undated",
   bm.tv_status({"first_air_date": "", "status": "In Production"}, T),
   {"state": "upcoming", "date": "", "what": "premiere"})
eq("next season announced",
   bm.tv_status({"first_air_date": "2020-01-01",
                 "next_episode_to_air": {"season_number": 3, "episode_number": 1,
                                         "air_date": "2026-11-02"}}, T),
   {"state": "upcoming", "date": "2026-11-02", "what": "season", "season": 3})
eq("mid-season weekly episode is not a wait",
   bm.tv_status({"first_air_date": "2020-01-01",
                 "next_episode_to_air": {"season_number": 3, "episode_number": 4,
                                         "air_date": "2026-10-02"}}, T)["state"], "out")
eq("season 1 episode 1 of an aired show is not a new season",
   bm.tv_status({"first_air_date": "2020-01-01",
                 "next_episode_to_air": {"season_number": 1, "episode_number": 1,
                                         "air_date": "2026-10-02"}}, T)["state"], "out")
eq("ended show is out", bm.tv_status({"first_air_date": "2010-01-01", "status": "Ended"}, T)["state"], "out")

# ── advance: the notification ────────────────────────────────────────────────
NOW = "2026-09-28T12:00:00Z"
waiting = {"state": "theaters", "date": "2026-10-20", "what": "digital"}
out = {"state": "out", "date": "2026-10-20", "what": "digital"}

e = {}
eq("already out when bookmarked: a change (status recorded)", bm.advance(e, out, NOW), True)
eq("…but no dot", e.get("new"), None)
eq("…and nothing awaited", e.get("awaiting"), None)
eq("same status again is no change", bm.advance(e, out, NOW), False)

e = {}
bm.advance(e, waiting, NOW)
eq("waiting is awaited", e.get("awaiting"), waiting)
eq("no dot while waiting", e.get("new"), None)
eq("TMDb unreachable changes nothing", bm.advance(e, None, NOW), False)
eq("…still awaited", e.get("awaiting"), waiting)
eq("junk status changes nothing", bm.advance(e, {"state": "??"}, NOW), False)
moved = {"state": "theaters", "date": "2026-11-03", "what": "digital"}
eq("a date pushed back is a change", bm.advance(e, moved, NOW), True)
eq("…that only moves the countdown", (e.get("new"), e["awaiting"]["date"]), (None, "2026-11-03"))
eq("the release lands", bm.advance(e, out, NOW), True)
eq("…dot raised", e.get("new"), NOW)
eq("…nothing awaited any more", e.get("awaiting"), None)
eq("seeing it again later doesn't re-stamp", bm.advance(e, out, "2026-10-01T00:00:00Z"), False)
eq("…original stamp kept", e["new"], NOW)

# A show awaiting season 3 whose refresh missed the gap and now sees season 4 announced.
s3 = {"state": "upcoming", "date": "2026-10-01", "what": "season", "season": 3}
s4 = {"state": "upcoming", "date": "2027-10-01", "what": "season", "season": 4}
e = {}
bm.advance(e, s3, NOW)
bm.advance(e, s4, NOW)
eq("season skipped past is still news", bool(e.get("new")), True)
eq("…and season 4 is now awaited", e["awaiting"]["season"], 4)

# Upcoming film opens in theaters: still waiting, no dot.
e = {}
bm.advance(e, {"state": "upcoming", "date": "2026-12-18", "what": "release"}, NOW)
bm.advance(e, {"state": "theaters", "date": "", "what": "digital"}, NOW)
eq("opening in theaters is not the digital release", e.get("new"), None)
bm.advance(e, {"state": "out", "date": "2027-02-01", "what": "digital"}, NOW)
eq("…digital is", bool(e.get("new")), True)

# ── mark_seen / new_count ────────────────────────────────────────────────────
lst = [{"kind": "movie", "id": 1, "new": NOW}, {"kind": "tv", "id": 2, "new": NOW},
       {"kind": "tv", "id": 3}]
eq("new_count", bm.new_count(lst), 2)
eq("mark one seen", bm.mark_seen(lst, {"tv:2"}), True)
eq("…the other stays", bm.new_count(lst), 1)
eq("mark all seen", bm.mark_seen(lst), True)
eq("…none left", bm.new_count(lst), 0)
eq("nothing to clear is no change", bm.mark_seen(lst), False)

# ── days_until ───────────────────────────────────────────────────────────────
eq("days ahead", bm.days_until("2026-10-01", T), 3)
eq("today", bm.days_until(T, T), 0)
eq("past", bm.days_until("2026-09-27", T), -1)
eq("no date", bm.days_until("", T), None)

# ── ordered ──────────────────────────────────────────────────────────────────
lst = [
    {"id": 1, "added_at": "2026-09-01", "status": {"state": "out"}},
    {"id": 2, "added_at": "2026-09-02", "status": {"state": "theaters", "date": "2026-11-01"}},
    {"id": 3, "added_at": "2026-09-03", "status": {"state": "upcoming", "date": "2026-10-01"}},
    {"id": 4, "added_at": "2026-08-01", "status": {"state": "out"}, "new": NOW},
    {"id": 5, "added_at": "2026-09-05", "status": {"state": "out"}},
    {"id": 6, "added_at": "2026-09-04", "status": {"state": "theaters", "date": ""}},
    "junk",
]
eq("order: new, soonest countdown, undated wait, newest bookmark",
   [e["id"] for e in bm.ordered(lst)], [4, 3, 2, 6, 5, 1])

print("bookmarks: %d passed, %d failed" % (_PASS, len(_FAIL)))
for f in _FAIL:
    print("  FAIL " + f)
sys.exit(1 if _FAIL else 0)
