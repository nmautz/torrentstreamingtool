"""Unit tests for season folders named by title (`episodes.name_seasons`, and
the ordinal / plural folder words pass 1 learned beside it). 20.24.0, #84.

    python tests/test_seasonfolders.py      (or `make test`)

`fixtures/haikyu_animetime.json` is real: the 120-file list of Anime Time's
complete Haikyu!! pack and TMDb's seasons and aliases for the show, as the box
held them on 2026-10-14. Before this pass every file of it was season 0 and
`want_seasons: [1]` stopped with "Couldn't tell which files in this pack are
Season 1".
"""

import collections
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import episodes as ep           # noqa: E402

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


with open(os.path.join(HERE, "fixtures", "haikyu_animetime.json"), encoding="utf-8") as fh:
    FIX = json.load(fh)
NAMES = [FIX["title"], FIX["original_title"], "Haikyu!!"] + FIX["aka"]


def settle(paths, seasons, names):
    slots = ep.attribute_paths(paths)
    ep.name_seasons(slots, seasons, names)
    ep.resolve_absolute(slots, seasons)
    return slots


# ── 1. Pass 1's new words ────────────────────────────────────────────────────
eq("an ordinal season folder", ep.dir_season("[Anime Time] Haikyu!! 2nd Season"), 2)
eq("a spelled-out ordinal", ep.dir_season("Show Second Season"), 2)
eq("an ordinal needs the word season", ep.dir_season("Show 2nd Stage"), None)
eq("an ordinal is not a range", ep.dir_season("Show 2nd Season S03"), None)
eq("OADs is the OAD bucket", ep.dir_bucket("[Anime Time] Haikyuu!! OADs"), "OAD")
eq("OVAs is the OVA bucket", ep.dir_bucket("Show OVAs"), "OVA")
eq("an NC folder is Extras", ep.dir_bucket("NC"), "Extras")
eq("a word ending in nc is not", ep.dir_bucket("Monsters Inc"), "")


# ── 2. The real pack ─────────────────────────────────────────────────────────
slots = settle(FIX["paths"], FIX["all_seasons"], NAMES)
by = collections.defaultdict(list)
for p, s in zip(FIX["paths"], slots):
    by[(s["season"], s["bucket"])].append(s["episode"])
for n, count in ((1, 25), (2, 25), (3, 10), (4, 25)):
    eq("season %d holds episodes 1-%d" % (n, count),
       sorted(by[(n, "")]), list(range(1, count + 1)))
eq("the creditless openings and endings are Extras", len(by[(0, "Extras")]), 26)
eq("the films are Movies", len(by[(0, "Movies")]), 4)
eq("the OADs folder is OAD", len(by[(0, "OAD")]), 2)
eq("Riku vs. Kuu names no season and stays a spin-off",
   len(by[(0, "[Anime Time] Haikyuu!! Riku vs. Kuu")]), 2)
eq("nothing else is left at season 0",
   sorted(k for k in by if k[0] == 0),
   [(0, "Extras"), (0, "Movies"), (0, "OAD"), (0, "OVA"),
    (0, "[Anime Time] Haikyuu!! Riku vs. Kuu")])

# The season-4 folder opens with an ALIAS that is a whole season title
# ("Haikyuu!! TO THE TOP"). Taking that off would leave nothing: season 1.
eq("the shortest show name is the one taken off",
   ep._folder_rest("[Anime Time] Haikyuu!! To The Top", NAMES), ["to", "the", "top"])
eq("a shortened season name still names it (no 'School')",
   ep._season_by_name("karasuno high vs shiratorizawa academy".split(),
                      FIX["all_seasons"]), 3)


# ── 3. What must not move ────────────────────────────────────────────────────
AOT = [{"season": 1, "name": "Season 1", "episode_count": 25},
       {"season": 2, "name": "Season 2", "episode_count": 12}]
jh = settle(["X/[Anime Time] Attack on Titan Season 1/[Anime Time] Attack on Titan - 01.mkv",
             "X/[Anime Time] Attack On Titan Junior High/[Anime Time] Attack on Titan Junior High - 04.mkv"],
            AOT, ["Attack on Titan"])
eq("a spin-off named after no season stays one",
   (jh[1]["season"], jh[1]["bucket"]), (0, "[Anime Time] Attack On Titan Junior High"))

OP = [{"season": 1, "name": "East Blue", "episode_count": 61},
      {"season": 2, "name": "Alabasta", "episode_count": 77}]
op = settle(["R/[Grp] One Piece/[Grp] One Piece - 1068.mkv",
             "R/[Grp] One Piece Film Red/One Piece Film Red.mkv"], OP, ["One Piece"])
eq("an absolute run does not fit season 1 and stays", op[0]["season"], 0)

two = settle(["R/[A] Show/[A] Show - 01.mkv", "R/[B] Show/[B] Show - 01.mkv"],
             AOT, ["Show"])
eq("two folders that are both just the show: neither is season 1",
   [s["season"] for s in two], [0, 0])

own = settle(["R/Show S01/Show.S01E01.mkv", "R/[A] Show/[A] Show - 02.mkv"], AOT, ["Show"])
eq("season 1 already held by other files: the bare folder stays",
   own[1]["season"], 0)

eq("no TMDb seasons, no opinion",
   ep.name_seasons(ep.attribute_paths(["R/[A] Show/[A] Show - 01.mkv",
                                       "R/[A] Show Extras/x.mkv"]), [], ["Show"]), False)

gen = [{"season": 1, "name": "Season 1", "episode_count": 10},
       {"season": 2, "name": "Season 2", "episode_count": 10}]
eq("a folder never matches a season TMDb names only by number",
   ep._season_by_name(["season", "2"], gen), 0)

# Idempotent: the monitor rebuilds pass 1 every tick and re-runs this.
again = settle(FIX["paths"], FIX["all_seasons"], NAMES)
eq("the same files give the same answer", again, slots)


# ---------------------------------------------------------------- report
if _FAIL:
    print("FAILED %d of %d" % (len(_FAIL), _PASS + len(_FAIL)))
    for _f in _FAIL:
        print("  - " + _f)
    sys.exit(1)
print("seasonfolders: %d passed" % _PASS)
