"""Unit tests for `animemap.py`. Run with plain python, no deps:

    python tests/test_animemap.py       (or `make test`)

Same shape as test_tmdbcache.py: a list of cases and a counter. The fixture is
the real `anime-list-full.xml` markup for five shows, copied verbatim from the
table on 2026-09-18, so every expectation below is a claim about real data:

  * Hunter x Hunter (2011) — an absolute run TMDb split 62/74/12 and the release
    groups split 58/78/12.
  * 【OSHI NO KO】 — three cours TMDb folded into one 35-episode season.
  * Code Geass — the control. Real seasons, both grids agree, nothing may move.
  * Sousou no Frieren — the OSHI NO KO shape with a bigger offset.
  * One Piece — an absolute run with explicit per-TMDb-season windows.
"""

import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import animemap as am            # noqa: E402

_PASS = 0
_FAIL = []


def eq(name, got, want):
    global _PASS
    if got == want:
        _PASS += 1
    else:
        _FAIL.append("%s\n     got:  %r\n     want: %r" % (name, got, want))


XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<anime-list>
  <anime anidbid="8550" tvdbid="252322" defaulttvdbseason="a" tmdbtv="46298" tmdbseason="a">
    <name>Hunter x Hunter (2011)</name>
    <mapping-list>
      <mapping anidbseason="1" tvdbseason="1" start="1" end="58"/>
      <mapping anidbseason="1" tvdbseason="2" start="59" end="136" offset="-58"/>
      <mapping anidbseason="1" tvdbseason="3" start="137" end="148" offset="-136"/>
    </mapping-list>
  </anime>
  <anime anidbid="17449" tvdbid="421069" defaulttvdbseason="1" tmdbtv="203737" tmdbseason="1">
    <name>"Oshi no Ko"</name>
  </anime>
  <anime anidbid="18086" tvdbid="421069" defaulttvdbseason="2" tmdbtv="203737" tmdbseason="1" tmdboffset="11">
    <name>"Oshi no Ko" (2024)</name>
  </anime>
  <anime anidbid="18901" tvdbid="421069" defaulttvdbseason="3" tmdbtv="203737" tmdbseason="1" tmdboffset="24">
    <name>"Oshi no Ko" (2026)</name>
  </anime>
  <anime anidbid="4521" tvdbid="79525" defaulttvdbseason="1" tmdbtv="31724" tmdbseason="1">
    <name>Code Geass: Hangyaku no Lelouch</name>
    <mapping-list>
      <mapping anidbseason="0" tvdbseason="0" start="2" end="12" offset="12">;1-0;</mapping>
    </mapping-list>
  </anime>
  <anime anidbid="5373" tvdbid="79525" defaulttvdbseason="2" tmdbtv="31724" tmdbseason="2">
    <name>Code Geass: Hangyaku no Lelouch R2</name>
  </anime>
  <anime anidbid="17617" tvdbid="424536" defaulttvdbseason="1" tmdbtv="209867" tmdbseason="1">
    <name>Sousou no Frieren</name>
  </anime>
  <anime anidbid="18886" tvdbid="424536" defaulttvdbseason="2" tmdbtv="209867" tmdbseason="1" tmdboffset="28">
    <name>Sousou no Frieren (2026)</name>
  </anime>
  <anime anidbid="69" tvdbid="81797" defaulttvdbseason="a" tmdbtv="37854" tmdbseason="a">
    <name>One Piece</name>
    <mapping-list>
      <mapping anidbseason="1" tvdbseason="1" tmdbseason="1" start="1" end="61" offset="0"/>
      <mapping anidbseason="1" tvdbseason="2" tmdbseason="2" start="62" end="77" offset="-61"/>
      <mapping anidbseason="1" tvdbseason="3" tmdbseason="3" start="78" end="91" offset="-77"/>
    </mapping-list>
  </anime>
</anime-list>
"""

INDEX = am.parse(XML)
HXH = INDEX[46298]
ONK = INDEX[203737]
GEASS = INDEX[31724]
FRIEREN = INDEX[209867]
OP = INDEX[37854]

# TMDb's own grids, as /tv/{id} reports them (verified live 2026-09-18).
HXH_SEASONS = [{"season": 1, "episode_count": 62},
               {"season": 2, "episode_count": 74},
               {"season": 3, "episode_count": 12}]
ONK_SEASONS = [{"season": 0, "episode_count": 2},
               {"season": 1, "episode_count": 35}]
GEASS_SEASONS = [{"season": 0, "episode_count": 44},
                 {"season": 1, "episode_count": 25},
                 {"season": 2, "episode_count": 25}]
FRIEREN_SEASONS = [{"season": 1, "episode_count": 39}]
OP_SEASONS = [{"season": 1, "episode_count": 61},
              {"season": 2, "episode_count": 16},
              {"season": 3, "episode_count": 14}]


# ── parse ────────────────────────────────────────────────────────────────────
eq("shows indexed", sorted(INDEX), [31724, 37854, 46298, 203737, 209867])
eq("hxh one TV entry", len(HXH), 1)
eq("hxh spans seasons", HXH[0]["tmdb_season"], am.ABSOLUTE)
eq("hxh windows", [(m["tvdb_season"], m["start"], m["end"]) for m in am.windows(HXH)],
   [(1, 1, 58), (2, 59, 136), (3, 137, 148)])
eq("onk three cours", len(ONK), 3)
eq("onk offsets", sorted(e["tmdb_offset"] for e in ONK), [0, 11, 24])
eq("a non-anime id is absent", INDEX.get(1396), None)

# ── show shape ───────────────────────────────────────────────────────────────
eq("hxh is an absolute run", am.is_absolute_run(HXH), True)
eq("one piece is an absolute run", am.is_absolute_run(OP), True)
eq("onk is not", am.is_absolute_run(ONK), False)
eq("code geass is not", am.is_absolute_run(GEASS), False)
eq("onk S2 is a folded cour", am.cour_target(ONK, 2), (1, 11))
eq("onk S3 is a folded cour", am.cour_target(ONK, 3), (1, 24))
eq("onk S1 is a no-op cour", am.cour_target(ONK, 1), (1, 0))
eq("frieren S2 is a folded cour", am.cour_target(FRIEREN, 2), (1, 28))
eq("geass S2 maps to itself", am.cour_target(GEASS, 2), (2, 0))
eq("an absolute run has no cour", am.cour_target(HXH, 2), None)
eq("unknown season has no cour", am.cour_target(ONK, 9), None)

# ── absolute ↔ TMDb grid ─────────────────────────────────────────────────────
eq("abs 1", am.from_absolute(1, HXH_SEASONS), (1, 1))
eq("abs 62 ends season 1", am.from_absolute(62, HXH_SEASONS), (1, 62))
eq("abs 63 opens season 2", am.from_absolute(63, HXH_SEASONS), (2, 1))
eq("abs 148 ends the show", am.from_absolute(148, HXH_SEASONS), (3, 12))
eq("abs 149 is off the end", am.from_absolute(149, HXH_SEASONS), None)
eq("round trip S2E1", am.to_absolute(2, 1, HXH_SEASONS), 63)
eq("round trip S1E59", am.to_absolute(1, 59, HXH_SEASONS), 59)
eq("round trip S3E12", am.to_absolute(3, 12, HXH_SEASONS), 148)
eq("past a season's end", am.to_absolute(1, 63, HXH_SEASONS), None)
eq("unknown season", am.to_absolute(9, 1, HXH_SEASONS), None)
eq("hxh total", am.total_episodes(HXH_SEASONS), 148)
eq("season 0 is not in the run", am.total_episodes(ONK_SEASONS), 35)


# ── decode_pack: the real releases ───────────────────────────────────────────
def ends(pairs):
    return None if pairs is None else (pairs[0], pairs[-1], len(pairs))


# iAHD's S01 — 58 files, already right, and it must stay right.
eq("iAHD S01 (owned, unchanged)",
   ends(am.decode_pack(HXH, HXH_SEASONS, 1, list(range(1, 59)))),
   ((1, 1), (1, 58), 58))
# iAHD's S02 — S02E01..S02E78 is really absolute 59..136.
eq("iAHD S02 (the off-by-four pack)",
   ends(am.decode_pack(HXH, HXH_SEASONS, 2, list(range(1, 79)))),
   ((1, 59), (2, 74), 78))
eq("iAHD S03",
   ends(am.decode_pack(HXH, HXH_SEASONS, 3, list(range(1, 13)))),
   ((3, 1), (3, 12), 12))
# ZigZag's Netflix S02 — a six-season split with ABSOLUTE numbers inside.
eq("ZigZag S02 (absolute numbers, foreign label)",
   ends(am.decode_pack(HXH, HXH_SEASONS, 2, list(range(27, 39)))),
   ((1, 27), (1, 38), 12))
# A scene release calling the whole run season 1 needs no help below 62 and
# must not be mangled above it.
eq("scene S01E59 (already correct)",
   ends(am.decode_pack(HXH, HXH_SEASONS, 1, [59, 60, 61, 62])),
   ((1, 59), (1, 62), 4))

eq("OSHI NO KO S02",
   ends(am.decode_pack(ONK, ONK_SEASONS, 2, list(range(1, 14)))),
   ((1, 12), (1, 24), 13))
eq("OSHI NO KO S03",
   ends(am.decode_pack(ONK, ONK_SEASONS, 3, list(range(1, 12)))),
   ((1, 25), (1, 35), 11))
eq("Frieren S02",
   ends(am.decode_pack(FRIEREN, FRIEREN_SEASONS, 2, list(range(1, 12)))),
   ((1, 29), (1, 39), 11))
eq("One Piece S02",
   ends(am.decode_pack(OP, OP_SEASONS, 2, list(range(1, 17)))),
   ((2, 1), (2, 16), 16))

# The control: Code Geass R2 is a real season 2 and comes back untouched.
eq("Code Geass R2 (control)",
   ends(am.decode_pack(GEASS, GEASS_SEASONS, 2, list(range(1, 26)))),
   ((2, 1), (2, 25), 25))
eq("Code Geass S1 (control)",
   ends(am.decode_pack(GEASS, GEASS_SEASONS, 1, list(range(1, 26)))),
   ((1, 1), (1, 25), 25))

# ── decode_pack: refusals ────────────────────────────────────────────────────
eq("no entries, no opinion", am.decode_pack([], HXH_SEASONS, 1, [1]), None)
eq("no numbers, no opinion", am.decode_pack(HXH, HXH_SEASONS, 1, []), None)
eq("season 0 is never decoded", am.decode_pack(HXH, HXH_SEASONS, 0, [1, 2]), None)
eq("a season the table doesn't know",
   am.decode_pack(HXH, HXH_SEASONS, 7, list(range(1, 13))), None)
# All-or-nothing: one file off the end refuses the whole pack rather than
# remapping most of it and dropping the rest.
eq("overlong pack refused",
   am.decode_pack(HXH, HXH_SEASONS, 3, list(range(1, 20))), None)
eq("a cour running past TMDb's season refused",
   am.decode_pack(ONK, ONK_SEASONS, 3, list(range(1, 30))), None)
# No grid to land on.
eq("no TMDb seasons, no opinion", am.decode_pack(HXH, [], 2, [1, 2]), None)


# ── remap_slots ──────────────────────────────────────────────────────────────
def slots(season, numbers, bucket=""):
    return [{"season": season, "episode": n, "bucket": bucket, "abs": False}
            for n in numbers]


s = slots(2, range(1, 79))
eq("remap reports the change", am.remap_slots(s, HXH_SEASONS, HXH), True)
eq("first file", (s[0]["season"], s[0]["episode"], s[0]["abs_no"]), (1, 59, 59))
eq("the S1/S2 seam", (s[3]["season"], s[3]["episode"]), (1, 62))
eq("first of TMDb S2", (s[4]["season"], s[4]["episode"], s[4]["abs_no"]), (2, 1, 63))
eq("last file", (s[-1]["season"], s[-1]["episode"], s[-1]["abs_no"]), (2, 74, 136))
eq("remap is idempotent", am.remap_slots(s, HXH_SEASONS, HXH), False)

s = slots(1, range(1, 59))
eq("owned pack is stamped", am.remap_slots(s, HXH_SEASONS, HXH), True)
eq("owned pack keeps its slots", (s[0]["season"], s[0]["episode"],
                                  s[-1]["season"], s[-1]["episode"]), (1, 1, 1, 58))
eq("and gains absolute numbers", s[-1].get("abs_no"), 58)
eq("stamping is idempotent too", am.remap_slots(s, HXH_SEASONS, HXH), False)

s = slots(2, range(1, 26))
eq("control is stamped", am.remap_slots(s, GEASS_SEASONS, GEASS), True)
eq("control keeps its slot", (s[0]["season"], s[0]["episode"]), (2, 1))
eq("control absolute number", s[0].get("abs_no"), 26)   # 25 in season 1, then this

# Pass 2 got there first: a fansub batch numbered 059-075 with no season folder
# comes out of `episodes.resolve_absolute` as S1E59..S1E62 + S2E1..S2E13, which
# are TMDb slots, not release labels. Reading them as labels slid season 2 down
# to S2E9. Pass 3 must record the absolute numbers and otherwise keep its hands
# off. (The `abs_resolved` mark is transient; `abs_no` is what survives a
# reload, which is why it has to be written here.)
s = ([{"season": 1, "episode": n, "bucket": "", "abs": False, "abs_resolved": True}
      for n in range(59, 63)]
     + [{"season": 2, "episode": n, "bucket": "", "abs": False, "abs_resolved": True}
        for n in range(1, 14)])
eq("pass 2's work is only stamped", am.remap_slots(s, HXH_SEASONS, HXH), True)
eq("pass 2's slots stand", [(x["season"], x["episode"]) for x in (s[0], s[3], s[4], s[-1])],
   [(1, 59), (1, 62), (2, 1), (2, 13)])
eq("with absolute numbers", [s[0]["abs_no"], s[-1]["abs_no"]], [59, 75])
eq("and then it settles", am.remap_slots(s, HXH_SEASONS, HXH), False)

# Still-flagged `abs` (pass 2 had no counts to resolve it with) is equally
# not a release label.
s = [{"season": 2, "episode": n, "bucket": "", "abs": True} for n in range(1, 11)]
eq("an unresolved abs slot is left alone", am.remap_slots(s, HXH_SEASONS, HXH), True)
eq("its numbers stand", (s[0]["season"], s[0]["episode"]), (2, 1))

s = slots(0, [1, 2, 3], bucket="OVA")
eq("buckets are never touched", am.remap_slots(s, HXH_SEASONS, HXH), False)
eq("no absolute number for a bucket", s[0].get("abs_no"), None)

s = slots(2, range(1, 79)) + slots(0, [1], bucket="Specials")
eq("a mixed item remaps only the run", am.remap_slots(s, HXH_SEASONS, HXH), True)
eq("special stayed put", (s[-1]["season"], s[-1]["episode"]), (0, 1))

eq("no table, no change", am.remap_slots(slots(2, range(1, 79)), HXH_SEASONS, []), False)


# ── the file store ───────────────────────────────────────────────────────────
NOW = time.mktime((2026, 9, 18, 12, 0, 0, 0, 0, -1))
root = tempfile.mkdtemp(prefix="animemap-test-")
try:
    m = am.AnimeMap(os.path.join(root, "store"))
    eq("empty store has no age", m.age(now=NOW), None)
    eq("empty store is stale", m.stale(now=NOW), True)
    eq("empty store has no entries", m.entries_for(46298), [])

    eq("a short body is refused", m.store(b"<anime-list/>"), False)
    eq("an HTML error page is refused", m.store(b"<html>" + b"x" * am.MIN_BYTES), False)
    padded = XML + b"<!--" + b"x" * am.MIN_BYTES + b"-->"
    eq("a real table stores", m.store(padded), True)
    eq("stored file is on disk", m.path.exists(), True)
    eq("entries come back", len(m.entries_for(46298)), 1)
    eq("non-anime stays empty", m.entries_for(1396), [])
    eq("fresh copy isn't stale", m.stale(now=(m.mtime() or NOW) + 60), False)
    eq("a week old is stale",
       m.stale(now=(m.mtime() or NOW) + am.REFRESH_AFTER + 1), True)

    # A failed refresh must leave the good table in place.
    eq("garbage refused", m.store(b"not xml at all" * 50000), False)
    eq("good table survived", len(m.entries_for(46298)), 1)

    # The parse is memoised on mtime, and a rewrite is picked up.
    m2 = am.AnimeMap(m.root)
    eq("a second reader parses from disk", len(m2.entries_for(203737)), 3)

    eq("an unwritable root is harmless",
       am.AnimeMap(os.path.join(root, "\0bad")).entries_for(46298), [])
finally:
    shutil.rmtree(root, ignore_errors=True)

print("animemap: %d passed, %d failed" % (_PASS, len(_FAIL)))
for f in _FAIL:
    print("  FAIL " + f)
sys.exit(1 if _FAIL else 0)
