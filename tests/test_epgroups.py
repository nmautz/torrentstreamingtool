"""Unit tests for `epgroups.py` - TMDb episode groups as views, and the specials
they place inside a season.

    python tests/test_epgroups.py      (or `make test`)

`fixtures/epgroups_tmdb.json` is the real TMDb data, fetched 2026-09-24 and
trimmed to what the rules read: Attack on Titan (12 groups), Firefly (2),
Breaking Bad (1), Game of Thrones (1), Hunter x Hunter (10). Group entries are
stored compactly as `[season, episode, order(, name)]`.
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import episodes                 # noqa: E402
import epgroups as eg           # noqa: E402

_PASS = 0
_FAIL = []


def ok(name, cond, detail=""):
    global _PASS
    if cond:
        _PASS += 1
    else:
        _FAIL.append("%s%s" % (name, ("\n     " + detail) if detail else ""))


def eq(name, got, want):
    ok(name, got == want, "got %r, want %r" % (got, want))


with open(os.path.join(HERE, "fixtures", "epgroups_tmdb.json"), encoding="utf-8") as fh:
    FIX = json.load(fh)


def detail(g):
    """A compact fixture group back into TMDb's own shape."""
    return {"id": g["id"], "name": g["name"], "type": g["type"], "groups": [
        {"name": b["name"], "order": b["order"], "episodes": [
            {"season_number": e[0], "episode_number": e[1], "order": e[2],
             "name": e[3] if len(e) > 3 else ""} for e in b["episodes"]]}
        for b in g["groups"]]}


def show(tv):
    s = FIX[str(tv)]
    return s, [eg.normalize(detail(g)) for g in s["details"]]


def F(name, season=0, episode=0, **kw):
    return {"name": name, "path": "/m/" + name, "season": season, "episode": episode, **kw}


# ── summarize: the picker's rows ─────────────────────────────────────────────
aot, AOT = show(1429)
rows = eg.summarize(aot["listing"])
eq("summarize: every group", len(rows), 12)
eq("summarize: story arcs lead", rows[0]["kind"], "Story arc")
eq("summarize: absolute lists last", rows[-1]["kind"], "Absolute")
arcs = next(r for r in rows if r["name"] == "Story Arcs")
eq("summarize: counts", (arcs["groups"], arcs["episodes"]), (11, 89))
eq("summarize: id is a string", arcs["id"], "63469b5dd34eb3007e7bce8a")
eq("summarize: junk tolerated", eg.summarize({"results": [None, {"name": "x"}]}), [])
eq("summarize: nothing", eg.summarize(None), [])

# ── normalize: buckets and episodes in THEIR order, not TMDb's array order ───
n = next(g for g in AOT if g["name"] == "Story Arcs")
eq("normalize: bucket order", [b["name"] for b in n["buckets"][:3]],
   ["The Fall of Shiganshina", "Humanity's Comeback", "The Struggle for Trost"])
last = n["buckets"][-1]["episodes"]
eq("normalize: finale specials close the last arc",
   [(e["season"], e["episode"]) for e in last[-2:]], [(0, 36), (0, 37)])
eq("normalize: episode shaped like metadata.seasons", sorted(last[0].keys()),
   ["air_date", "episode", "name", "overview", "runtime", "season", "still_path"])
eq("normalize: garbage", eg.normalize({"name": "no id"}), None)

# ── season_homes: only a WHOLE-season bucket votes ───────────────────────────
H = eg.season_homes(AOT, aot["all_seasons"])
eq("AoT: the Final Chapters specials belong to season 4",
   [(h["episode"], h["after"]) for h in H.get(4, [])], [(36, 28), (37, 28)])
eq("AoT: nothing else moves", sorted(H), [4])
eq("AoT: special keeps its TMDb name", H[4][0]["name"], "The Final Chapters Special (1)")

# Story arcs alone cut season 4 in two, so they can't vote.
arcs_only = [g for g in AOT if g["type"] == 5]
eq("story arcs never vote", eg.season_homes(arcs_only, aot["all_seasons"]), {})

ff, FF = show(1437)
H = eg.season_homes(FF, ff["all_seasons"])
eq("Firefly: three unaired episodes close season 1",
   [(h["episode"], h["after"]) for h in H.get(1, [])], [(1, 11), (2, 11), (3, 11)])

for tv, label in ((1396, "Breaking Bad"), (1399, "Game of Thrones"), (46298, "Hunter x Hunter")):
    s, G = show(tv)
    eq("%s: nothing moves" % label, eg.season_homes(G, s["all_seasons"]), {})

# A dissenting whole-season bucket vetoes.
SEAS = [{"season": 1, "episode_count": 2}, {"season": 2, "episode_count": 2}]


def grp(*buckets):
    return eg.normalize({"id": "g", "groups": [
        {"order": i, "name": "b%d" % i, "episodes": [
            {"season_number": s, "episode_number": e, "order": j} for j, (s, e) in enumerate(b)]}
        for i, b in enumerate(buckets)]})


a = grp([(1, 1), (0, 5), (1, 2)], [(2, 1), (2, 2)])
b = grp([(1, 1), (1, 2)], [(2, 1), (2, 2), (0, 5)])
eq("mid-season position", eg.season_homes([a], SEAS), {1: [{"episode": 5, "after": 1, "name": ""}]})
eq("disagreement: dropped", eg.season_homes([a, b], SEAS), {})
eq("a partial season can't vote", eg.season_homes([grp([(1, 1), (0, 5)])], SEAS), {})
eq("a mixed bucket can't vote",
   eg.season_homes([grp([(1, 1), (1, 2), (2, 1), (0, 5)])], SEAS), {})

# ── trailing_number ──────────────────────────────────────────────────────────
eq("trail: finale", eg.trailing_number(F("[Anime Time] Attack on Titan Season 4 - Finale 2.mkv")), 2)
eq("trail: season tail is not one", eg.trailing_number(F("Show Season 4.mkv")), 0)
eq("trail: none", eg.trailing_number(F("Show - Recap.mkv")), 0)
eq("trail: a year is not one", eg.trailing_number(F("Show 2023.mkv")), 0)

# ── place_files: the Attack on Titan pack, as it sits on the box ─────────────
H = eg.season_homes(AOT, aot["all_seasons"])


def aot_files():
    fs = [F("[Anime Time] Attack on Titan - %02d.mkv" % (59 + i), 4, i, abs_no=59 + i)
          for i in range(1, 29)]
    fs += [F("[Anime Time] Attack on Titan Season 4 - Finale 1.mkv", 4, 0),
           F("[Anime Time] Attack on Titan Season 4 - Finale 2.mkv", 4, 0),
           F("[Anime Time] Attack on Titan OAD - 01.mkv", 0, 1, bucket="OAD")]
    return fs


fs = aot_files()
ok("eligible", eg.eligible(fs))
ok("placed", eg.place_files(fs, H, "Attack on Titan"))
fin = [f for f in fs if "Finale" in f["name"]]
eq("Finale 1 is S00E36", (fin[0]["season"], fin[0]["episode"]), (0, 36))
eq("Finale 2 is S00E37", (fin[1]["season"], fin[1]["episode"]), (0, 37))
eq("placed files remember where they came from", fin[0]["home"],
   {"season": 4, "after": 28, "placed": True})
oad = fs[-1]
eq("a bucketed OAD is untouched", (oad["season"], oad["episode"], "home" in oad), (0, 1, False))
ok("idempotent", not eg.place_files(fs, H, "Attack on Titan"))
order = [f["name"] for f in sorted(fs, key=episodes.sort_key)]
eq("play order: S04E28, then the finales, then the OAD bucket",
   [n[-22:] for n in order[27:31]],
   ["Attack on Titan - 87.mkv"[-22:], "on Season 4 - Finale 1.mkv"[-22:],
    "on Season 4 - Finale 2.mkv"[-22:], "Attack on Titan OAD - 01.mkv"[-22:]])
ok("reset rewinds", eg.reset_files(fs))
eq("reset: back to (4, 0)", [(f["season"], f["episode"]) for f in fs if "Finale" in f["name"]],
   [(4, 0), (4, 0)])
ok("reset: no homes left", not any("home" in f for f in fs))

# Only Finale 2 on disk: numbered, so it still knows which special it is.
fs = [F("Attack on Titan Season 4 - Finale 2.mkv", 4, 0)]
eg.place_files(fs, H, "Attack on Titan")
eq("one of two, by number", (fs[0]["season"], fs[0]["episode"]), (0, 37))

# Three stuck files for two specials: refuse the lot.
fs = [F("AoT S4 - Finale 1.mkv", 4, 0), F("AoT S4 - Finale 2.mkv", 4, 0),
      F("AoT S4 - Finale 3.mkv", 4, 0)]
ok("too many: nothing moves", not eg.place_files(fs, H, "Attack on Titan"))

# Unnumbered: by order only when each file shares a real word with its special.
fs = [F("Attack on Titan - The Final Chapters Part One.mkv", 4, 0),
      F("Attack on Titan - The Final Chapters Part Two.mkv", 4, 0)]
eg.place_files(fs, H, "Attack on Titan")
eq("unnumbered, word evidence", [(f["season"], f["episode"]) for f in fs], [(0, 36), (0, 37)])
fs = [F("Attack on Titan - Recap A.mkv", 4, 0), F("Attack on Titan - Recap B.mkv", 4, 0)]
ok("unnumbered, no evidence: refused", not eg.place_files(fs, H, "Attack on Titan"))

# Never onto a special some file already is.
fs = [F("Attack on Titan S00E36.mkv", 0, 36), F("AoT Season 4 - Finale 1.mkv", 4, 0)]
eg.place_files(fs, H, "Attack on Titan")
eq("target held: the stuck file stays", (fs[1]["season"], fs[1]["episode"]), (4, 0))
eq("…and the real S00E36 gets its home", fs[0].get("home"), {"season": 4, "after": 28})

# A special that ALREADY carries its number gets a home, and loses it when the
# groups stop agreeing. A MOVED file keeps its home (no flicker on a thin cache).
fs = [F("Firefly S00E01.mkv", 0, 1), F("Firefly S00E09.mkv", 0, 9)]
ok("numbered specials homed", eg.place_files(fs, eg.season_homes(FF, ff["all_seasons"])))
eq("Firefly special after S01E11", fs[0].get("home"), {"season": 1, "after": 11})
ok("an unplaced special has no home", "home" not in fs[1])
ok("homes withdrawn", eg.place_files(fs, {}))
ok("…and the stamp is gone", "home" not in fs[0])
moved = [F("AoT Season 4 - Finale 1.mkv", 0, 36, home={"season": 4, "after": 28, "placed": True})]
ok("a moved file keeps its home", not eg.place_files(moved, {}) and "home" in moved[0])

ok("eligible: nothing to do", not eg.eligible([F("x S01E01.mkv", 1, 1),
                                               F("OAD - 01.mkv", 0, 1, bucket="OAD")]))

print("%d passed, %d failed" % (_PASS, len(_FAIL)))
for f in _FAIL:
    print("  FAIL:", f)
sys.exit(1 if _FAIL else 0)
