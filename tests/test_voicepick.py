"""Unit tests for `voicepick.py`. Run with plain python, no deps:

    python tests/test_voicepick.py      (or `make test`)

A voice download has nobody watching the screen. The cases lean on the two
ways that goes wrong: starting the wrong FILM (a sequel, a remake, a title
that happens to contain "first" or a year), and starting the wrong COPY (an
episode, a sibling film that shares the words, a release that plays green).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import voicepick as vp          # noqa: E402

_PASS = 0
_FAIL = []


def eq(name, got, want):
    global _PASS
    if got == want:
        _PASS += 1
    else:
        _FAIL.append("%s\n     got:  %r\n     want: %r" % (name, got, want))


def req(s):
    r = vp.parse_request(s)
    return (r["query"], r["year"], r["want"])


# ── what was said ─────────────────────────────────────────────────────────────
eq("plain", req("Star Wars"), ("Star Wars", 0, ""))
eq("the original one", req("Star Wars, the original one"), ("Star Wars", 0, "oldest"))
eq("the original", req("Star Wars the original"), ("Star Wars", 0, "oldest"))
eq("the first one", req("dune the first one"), ("dune", 0, "oldest"))
eq("the new one", req("Dune the new one."), ("Dune", 0, "newest"))
eq("the remake", req("Total Recall the remake"), ("Total Recall", 0, "newest"))
eq("trailing year", req("Dune 1984"), ("Dune", 1984, ""))
eq("from year", req("Dune from 1984"), ("Dune", 1984, ""))
eq("the year one", req("dune the 1984 one"), ("dune", 1984, ""))
eq("year in brackets", req("Dune (2021)"), ("Dune", 2021, ""))
eq("year and hint", req("Dune 1984 the original"), ("Dune", 1984, "oldest"))
eq("the movie lead-in", req("the movie Heat"), ("Heat", 0, ""))
eq("a year that IS the title", req("1917"), ("1917", 0, ""))
eq("a hint that IS the title", req("The Remake"), ("The Remake", 0, ""))
eq("'first' inside a title", req("First Blood"), ("First Blood", 0, ""))
eq("'old' as a title", req("Old"), ("Old", 0, ""))
eq("'original' leading a title", req("The Original Kings of Comedy"),
   ("The Original Kings of Comedy", 0, ""))
eq("'the film' with nothing after it", req("the film"), ("the film", 0, ""))
eq("empty", req(""), ("", 0, ""))

# ── which film ────────────────────────────────────────────────────────────────
SW = [{"title": "Star Wars: The Force Awakens", "year": "2015", "kind": "movie", "votes": 19000},
      {"title": "Star Wars", "year": "1977", "kind": "movie", "votes": 21000},
      {"title": "Rogue One: A Star Wars Story", "year": "2016", "kind": "movie", "votes": 15000},
      {"title": "A Christmas in New Hope", "year": "2024", "kind": "movie", "votes": 12}]
DUNE = [{"title": "Dune", "year": "2021", "kind": "movie", "votes": 13000},
        {"title": "Dune: Part Two", "year": "2024", "kind": "movie", "votes": 7000},
        {"title": "Dune", "year": "1984", "kind": "movie", "votes": 3000},
        {"title": "The Dune", "year": "2025", "kind": "movie", "votes": 4},
        {"title": "Dune", "year": "", "kind": "movie", "votes": 0}]


def pick(s, cands):
    c, sure = vp.choose_title(vp.parse_request(s), cands)
    return (c and (c["title"], c["year"]), sure)


eq("exact name beats the more popular sequel", pick("star wars", SW), (("Star Wars", "1977"), True))
eq("original one", pick("star wars the original one", SW), (("Star Wars", "1977"), True))
eq("same name: popularity decides", pick("dune", DUNE), (("Dune", "2021"), True))
eq("same name: the original", pick("dune the original one", DUNE), (("Dune", "1984"), True))
eq("same name: the new one is not an unknown namesake", pick("dune the new one", DUNE), (("Dune", "2021"), True))
eq("same name: by year", pick("dune 1984", DUNE), (("Dune", "1984"), True))
eq("a year reaches the unknown namesake", pick("dune 2025", DUNE), (("The Dune", "2025"), True))
eq("a year nobody has is not guessed", pick("dune 1999", DUNE), (None, False))
eq("an alternative title: the famous film, flagged unsure, not an obscure name match",
   pick("a new hope", SW), (("Star Wars", "1977"), False))
eq("the obscure film's own full title still gets it",
   pick("a christmas in new hope", SW), (("A Christmas in New Hope", "2024"), True))
eq("no name match at all: the best known, unsure", pick("zzzz", SW), (("Star Wars", "1977"), False))
eq("no votes anywhere: TMDb's order",
   pick("zzzz", [{"title": "B", "year": "2000"}, {"title": "A", "year": "1990"}]), (("B", "2000"), False))
eq("nothing found", pick("zzzz", []), (None, False))

OFFICE_F = {"title": "The Office", "year": "1966", "kind": "movie", "votes": 9}
OFFICE_S = {"title": "The Office", "year": "2005", "kind": "tv", "votes": 4500}
FARGO_F = {"title": "Fargo", "year": "1996", "kind": "movie", "votes": 8000}
FARGO_S = {"title": "Fargo", "year": "2014", "kind": "tv", "votes": 2500}
eq("an obscure film under a famous series: the series", vp.series_meant(OFFICE_F, True, OFFICE_S, True), True)
eq("a real film and a series: the film", vp.series_meant(FARGO_F, True, FARGO_S, True), False)
eq("only a series matched", vp.series_meant(FARGO_F, False, FARGO_S, True), True)
eq("no film at all", vp.series_meant(None, False, FARGO_S, True), True)
eq("series only loosely matched", vp.series_meant(FARGO_F, False, FARGO_S, False), False)

# ── which copy ────────────────────────────────────────────────────────────────
M = "magnet:?xt=urn:btih:"


def r(title, seeders, rel=1.0, kind="movie", **kw):
    return dict({"title": title, "magnet": M + title, "seeders": seeders, "rel": rel, "kind": kind}, **kw)


eq("availability buckets", [vp.avail_rank(n) for n in (0, 1, 8, 9, 30, 31, 400)], [0, 1, 1, 2, 2, 3, 3])
eq("heights", [vp.height(t) for t in ("A.1080p.x", "A 720i", "A 4K HDR", "A.UHD", "A", "A.21600p")],
   [1080, 720, 2160, 2160, 0, 0])

res = [r("Star.Wars.1977.720p", 400),
       r("Star.Wars.1977.1080p.DV", 900, dv_risk=True),
       r("Star.Wars.1977.1080p.DUAL", 40, tracks=2),
       r("Star.Wars.The.Force.Awakens.2015.1080p", 5000, rel=0.6),
       r("Star.Wars.Rebels.S01E01", 800, kind="episode"),
       r("Star.Wars.1977.480p", 3),
       {"title": "no magnet", "seeders": 9999, "rel": 1.0, "kind": "movie"}]
order = [x["title"] for x in vp.candidates(res)]
eq("richer tracks win inside a bucket; green last; sequel, episode, linkless dropped",
   order, ["Star.Wars.1977.1080p.DUAL", "Star.Wars.1977.720p", "Star.Wars.1977.480p",
           "Star.Wars.1977.1080p.DV"])
eq("nothing relevant enough is nothing", vp.candidates([r("Other.Film.2001", 900, rel=0.5)]), [])
eq("the band follows the best", [x["title"] for x in vp.candidates(
    [r("A", 5, rel=1.0), r("B", 500, rel=0.9), r("C", 900, rel=0.8)])], ["B", "A"])
eq("a missing rel is not relevance", vp.candidates([dict(r("A", 5), rel=None)]), [])

pool = vp.candidates([r("Film.2000.720p", 400), r("Film.2000.1080p", 20), r("Film.2000.2160p", 300),
                      r("Film.2000.720p", 100), r("Film.2000.DVDRip", 0), r("Film.2000.576p", 50)])
eq("race: pick, then the best picture under the ceiling, then the rest; dead and dupes out",
   [x["title"] for x in vp.shortlist(pool, 1080)],
   ["Film.2000.720p", "Film.2000.1080p", "Film.2000.2160p", "Film.2000.576p"])
eq("race: a dead pick keeps the dead ones", [x["title"] for x in vp.shortlist(
    vp.candidates([r("A.720p", 0), r("B.480p", 0)]))], ["A.720p", "B.480p"])
eq("race: nothing", vp.shortlist([]), [])
eq("race: capped", len(vp.shortlist(vp.candidates([r("F.%d" % i, 50 + i) for i in range(20)]))),
   vp.SHORTLIST_MAX)

if _FAIL:
    print("FAILED %d (passed %d)" % (len(_FAIL), _PASS))
    for f in _FAIL:
        print("  - " + f)
    sys.exit(1)
print("voicepick: %d passed" % _PASS)
