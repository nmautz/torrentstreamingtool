"""Unit tests for `titleslot.py` - which episode a release IS, by the name it states.

    python tests/test_titleslot.py      (or `make test`)

The season list is TMDb's real SpongeBob SquarePants season 2 (36 entries, read
off the box on 2026-10-06) and the release names are real indexer titles from
the same day: the AMZN per-segment set that runs one behind TMDb from E09, and
the SKST / MeGusta half-hours that hold two segments each.
"""

import copy
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import titleslot as ts            # noqa: E402

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


_S2 = ["Something Smells", "Bossy Boots", "Your Shoe's Untied", "Squid's Day Off",
       "Big Pink Loser", "Bubble Buddy", "Mermaid Man & Barnacle Boy III",
       "Squirrel Jokes", "Christmas Who?", "Dying for Pie", "Imitation Krabs",
       "Wormy", "Patty Hype", "Survival of the Idiots", "Dumped", "Grandma's Kisses",
       "Squidville", "No Free Rides", "I'm Your Biggest Fanatic", "Pressure",
       "The Smoking Peanut", "Shanghaied", "Prehibernation Week", "Life of Crime",
       "The Secret Box", "Band Geeks", "Sailor Mouth", "Artist Unknown",
       "Jellyfish Hunter", "The Fry Cook Games", "Just One Bite", "The Bully",
       "Squid on Strike", "Sandy, SpongeBob, and the Worm", "Procrastination",
       "I'm with Stupid"]
S2 = [{"episode": i + 1, "name": n} for i, n in enumerate(_S2)]


def S(name, eps=S2):
    r = ts.slot(name, eps)
    return (r["episode"], r["also"]) if r else None


# ── A release numbered like TMDb: no opinion ────────────────────────────────
eq("own name, own number", S("SpongeBob SquarePants S02E05 Big Pink Loser REPACK 1080p AMZN WEB-DL"), None)
eq("apostrophe dropped by the release", S("SpongeBob SquarePants S02E36 Im with Stupid REPACK 1080p"), None)
eq("plural differs from TMDb", S("SpongeBob SquarePants S02E29 Jellyfish Hunters REPACK 1080p AMZN"), None)
eq("'and' for TMDb's '&'", S("SpongeBob SquarePants S02E07 Mermaid Man and Barnacle Boy III REPACK 1080p"), None)
eq("no title stated", S("SpongeBob.SquarePants.S02E10.1080p.WEB-DL.x264-GRP.mkv"), None)
eq("no marker", S("SpongeBob SquarePants Dying for Pie 1080p"), None)

# ── Numbered one behind TMDb (AMZN from E09) ────────────────────────────────
eq("E09 is TMDb E10", S("SpongeBob SquarePants S02E09 Dying for Pie REPACK 1080p AMZN WEB-DL DDP2 0 H 264-Kitsune.mkv"), (10, []))
eq("E15 is TMDb E16", S("SpongeBob SquarePants S02E15 Grandmas Kisses REPACK 1080p AMZN WEB-DL DDP2"), (16, []))
eq("reordered: E21 is TMDb E20", S("SpongeBob SquarePants S02E21 Pressure REPACK 1080p AMZN WEB-DL"), (20, []))
eq("reordered: E19 is TMDb E22", S("SpongeBob SquarePants S02E19 Shanghaied REPACK 1080p AMZN"), (22, []))

# ── Two segments in one file ────────────────────────────────────────────────
eq("half-hour, HTML-escaped ampersand",
   S("SpongeBob SquarePants S02E09 Survival of the Idiots&amp;Dumped 1080p SKST WEB-DL"), (14, [15]))
eq("half-hour, dotted with 'and'",
   S("SpongeBob.SquarePants.S02E10.No.Free.Rides.and.Im.Your.Biggest.Fanatic.1080p.HEVC.x265-MeGusta"), (18, [19]))
eq("long names",
   S("SpongeBob SquarePants S02E20 Squid on Strike&amp;Sandy SpongeBob and the Worm 1080p SKST"), (33, [34]))
eq("hyphenated where TMDb joins the word",
   S("SpongeBob.SquarePants.S02E07.Pre-Hibernation.Week.and.Life.Of.Crime.1080p.HEVC"), (23, [24]))
eq("own number is the SECOND half: filed under the first",
   S("SpongeBob.SquarePants.S02E02.Something.Smells.and.Bossy.Boots.720p.HEVC.x265"), (1, [2]))
eq("own number is the first half: gains the second",
   S("SpongeBob SquarePants S02E01 Something Smells & Bossy Boots 1080p"), (1, [2]))
eq("one half TMDb does not list in this season",
   S("SpongeBob SquarePants S02E13 Shanghaied&amp;Gary Takes a Bath 1080p SKST WEB-DL"), (22, []))

# ── No answer is better than a guess ────────────────────────────────────────
eq("names nothing TMDb lists", S("SpongeBob SquarePants S02E14 Welcome to the Chum Bucket&amp;Frankendoodle 1080p"), None)
eq("a different title for the same special", S("SpongeBob SquarePants S02E08 The SpongeBob Christmas Special 1080p SKST"), None)
eq("empty season list", S("Show S02E09 Dying for Pie 1080p", []), None)
eq("season 0", S("Show S00E03 Dying for Pie 1080p"), None)

SHORT = [{"episode": 1, "name": "Opposite Day"}, {"episode": 2, "name": "Day"},
         {"episode": 3, "name": "Valentine's Day"}, {"episode": 4, "name": "Episode 4"}]
eq("a name with no 4-letter word identifies nothing", S("Show S01E03 Day 720p", SHORT), None)
eq("a placeholder name identifies nothing", S("Show S01E01 Episode 4 720p", SHORT), None)
eq("a name is a phrase, not a bag of words", S("Show S01E02 Day Opposite 720p", SHORT), None)

SHARE = [{"episode": 1, "name": "The Bully Returns"}, {"episode": 2, "name": "Returns"}]
eq("sharing a word with its own number's name is not a contradiction",
   S("Show S01E01 Returns 1080p", SHARE), None)

TWIN = [{"episode": 1, "name": "The Beginning"}, {"episode": 2, "name": "The Beginning"},
        {"episode": 3, "name": "Other Thing"}]
eq("two episodes with one name cannot be told apart", S("Show S01E03 The Beginning 1080p", TWIN), None)

NEST = [{"episode": 1, "name": "Shanghaied"}, {"episode": 2, "name": "Shanghaied Again"},
        {"episode": 3, "name": "Filler Name"}]
eq("a name inside a longer matched name is not a second episode",
   S("Show S01E03 Shanghaied Again 1080p", NEST), (2, []))

# ── One word missing from a long name ───────────────────────────────────────
S3 = [{"episode": 1, "name": "Mermaid Man and Barnacle Boy IV"},
      {"episode": 20, "name": "Chocolate with Nuts"},
      {"episode": 21, "name": "Mermaid Man and Barnacle Boy V"},
      {"episode": 22, "name": "Club SpongeBob"}]
eq("a long name with one word missing",
   S("SpongeBob SquarePants S03E20 Mermaid and Barnacle Boy V REPACK 1080p AMZN", S3), (21, []))
eq("... but not when two episodes could be meant",
   S("SpongeBob SquarePants S03E20 Mermaid Man and Barnacle Boy REPACK 1080p", S3), None)
eq("... and the exact name still wins",
   S("SpongeBob SquarePants S03E20 Mermaid Man and Barnacle Boy IV REPACK 1080p", S3), (1, []))
eq("a three-word name gets no tolerance",
   S("Show S03E22 Big Loser 1080p", S3 + [{"episode": 23, "name": "Big Pink Loser"}]), None)
eq("two words missing is not a match",
   S("SpongeBob SquarePants S03E20 Barnacle Boy V REPACK 1080p", S3), None)

eq("a hyphenated word is not a release tag", ts.stated("Show S01E01 Spider-Man Returns 1080p WEB-DL"),
   ["spider", "man", "return"])
eq("the group's hyphen is", ts.stated("Show S01E01 Pilot H 264-Kitsune"), ["pilot", "h"])
eq("range tail skipped", ts.stated("Show.S01E01-E02.Pilot.720p"), ["pilot"])

# ── held ────────────────────────────────────────────────────────────────────
eq("held: plain", ts.held({"episode": 3}), [3])
eq("held: with also", ts.held({"episode": 14, "also": [15]}), [14, 15])
eq("held: none", ts.held({"episode": 0}), [])


# ── place_files (pass 5) ────────────────────────────────────────────────────
def F(name, season, episode, **kw):
    d = {"name": name, "path": "/dl/" + name, "season": season, "episode": episode}
    d.update(kw)
    return d


SEAS = {"2": {"episodes": S2}}
files = [
    F("SpongeBob SquarePants S02E09 Dying for Pie REPACK 1080p AMZN WEB-DL DDP2 0 H 264-Kitsune.mkv", 2, 9),
    F("SpongeBob.SquarePants.S02E09.Survival.of.the.Idiots.and.Dumped.1080p.HEVC.x265-MeGusta.mkv", 2, 9),
    F("SpongeBob SquarePants S02E05 Big Pink Loser REPACK 1080p AMZN WEB-DL.mkv", 2, 5),
    F("SpongeBob SquarePants S02E14 Welcome to the Chum Bucket&Frankendoodle 1080p SKST.mkv", 2, 14),
]
ok("place: reports a change", ts.place_files(files, SEAS))
eq("place: moved by name", (files[0]["episode"], files[0].get("ts_from"), files[0].get("also")), (10, 9, None))
eq("place: half-hour", (files[1]["episode"], files[1].get("ts_from"), files[1].get("also")), (14, 9, [15]))
eq("place: untouched", (files[2]["episode"], "ts_from" in files[2], "also" in files[2]), (5, False, False))
eq("place: unplaceable stays", files[3]["episode"], 14)
snap = copy.deepcopy(files)
ok("place: second run changes nothing", not ts.place_files(files, SEAS))
eq("place: idempotent", files, snap)

# The download monitor rebuilds the list from the file names every tick.
rebuilt = [F(f["name"], 2, ts.marker(f["name"])[1]) for f in files]
ts.place_files(rebuilt, SEAS)
eq("place: same answer from a rebuilt list", [(f["episode"], f.get("also")) for f in rebuilt],
   [(f["episode"], f.get("also")) for f in files])

# Not this pass's files.
skip = [
    F("Show S02E09 Dying for Pie 1080p.mkv", 0, 9, bucket="Specials"),
    F("Show S02E09 Dying for Pie 1080p.mkv", 2, 9, abs_no=45),
    F("Show S02E09 Dying for Pie 1080p.mkv", 2, 9, rel_season=1, rel_episode=9),
    F("Show S02E09 Dying for Pie 1080p.mkv", 3, 9),          # something else placed it
    F("Show S02E09 Dying for Pie 1080p.mkv", 2, 12),         # ... or renumbered it
    F("Show - 09 - Dying for Pie.mkv", 2, 9),                # no SxxExx of its own
]
before = copy.deepcopy(skip)
ok("place: leaves other passes' files alone", not ts.place_files(skip, SEAS))
eq("place: ... unchanged", skip, before)

# No names for the season: no opinion, in either direction.
moved = [F("Show S02E09 Dying for Pie 1080p.mkv", 2, 10, ts_from=9)]
ok("place: no names, no undo", not ts.place_files(moved, {}))
eq("place: ... still moved", moved[0]["episode"], 10)
# TMDb renamed the episode: the reason is gone, so the file goes home.
renamed = {"2": {"episodes": [dict(e, name="Something Else Entirely") if e["episode"] == 10 else e
                              for e in S2]}}
ok("place: reason gone", ts.place_files(moved, renamed))
eq("place: ... back on its own number", (moved[0]["episode"], "ts_from" in moved[0]), (9, False))

# ── Across seasons, for the episode an item was fetched for (20.15.0) ───────
# Real AMZN titles: four season 2/3 episodes are released as specials, one
# under the wrong season.
S3N = [{"episode": 19, "name": "Party Pooper Pants"}, {"episode": 20, "name": "Chocolate with Nuts"},
       {"episode": 30, "name": "The Sponge Who Could Fly"}, {"episode": 31, "name": "Gary Takes a Bath"},
       {"episode": 36, "name": "Ugh"}]
ALLS = {"2": {"episodes": S2}, "3": {"episodes": S3N}}
X = lambda name, s, e, seas=ALLS: ts.cross(name, s, e, seas)
ok("special -> S02E09", X("SpongeBob SquarePants S00E01 Christmas Who 1080p AMZN WEB-DL DDP2 0 H 264-Kitsune", 2, 9))
ok("special -> S03E30", X("SpongeBob SquarePants S00E03 The Sponge Who Could Fly 1080p AMZN WEB-DL", 3, 30))
ok("special -> S03E19", X("SpongeBob SquarePants S00E02 Party Pooper Pants 1080p AMZN", 3, 19))
ok("special, short name at the end of the title", X("SpongeBob SquarePants S00E04 Spongebob BC Ugh 1080p AMZN WEB-DL", 3, 36))
ok("another season's number -> S03E31", X("SpongeBob SquarePants S02E20 Gary Takes a Bath REPACK 1080p AMZN", 3, 31))
ok("not for a different wanted episode", not X("SpongeBob SquarePants S00E01 Christmas Who 1080p", 3, 30))
ok("not when it is at home in its own season",
   not X("SpongeBob SquarePants S02E15 Dumped 1080p", 3, 31,
         {"2": {"episodes": S2}, "3": {"episodes": [{"episode": 31, "name": "Dumped"}]}}))
ok("not from a numbered season whose names are unknown",
   not X("SpongeBob SquarePants S02E20 Gary Takes a Bath 1080p", 3, 31, {"3": {"episodes": S3N}}))
ok("not within the same season", not X("SpongeBob SquarePants S03E05 Gary Takes a Bath 1080p", 3, 31))
ok("a short name in the middle of a title is not it",
   not X("SpongeBob SquarePants S00E09 Ugh What A Day 1080p", 3, 36))
ok("no stated title", not X("SpongeBob.SquarePants.S00E04.1080p.WEB-DL", 3, 36))

sp = [F("SpongeBob SquarePants S00E03 The Sponge Who Could Fly 1080p AMZN WEB-DL.mkv", 0, 3,
        home={"season": 3, "after": 29})]
ok("place: crosses for the wanted episode", ts.place_files(sp, ALLS, (3, 30)))
eq("place: ... filed there", (sp[0]["season"], sp[0]["episode"], sp[0]["ts_from_season"], sp[0]["ts_from"],
                              "home" in sp[0]), (3, 30, 0, 3, False))
ok("place: ... and stays", not ts.place_files(sp, ALLS, (3, 30)))
eq("place: ... still there", (sp[0]["season"], sp[0]["episode"]), (3, 30))
rb = [F(sp[0]["name"], 0, 3)]
ts.place_files(rb, ALLS, (3, 30))
eq("place: same answer from a rebuilt list", (rb[0]["season"], rb[0]["episode"]), (3, 30))
ok("place: no names for the wanted season, no undo", not ts.place_files(sp, {"2": {"episodes": S2}}, (3, 30)))
ok("place: item no longer wants it", ts.place_files(sp, ALLS, None))
eq("place: ... back where its number says", (sp[0]["season"], sp[0]["episode"], "ts_from_season" in sp[0]), (0, 3, False))
plain = [F("SpongeBob SquarePants S00E01 Christmas Who 1080p.mkv", 0, 1)]
ok("place: a special nobody fetched for an episode stays a special", not ts.place_files(plain, ALLS, None))
g = [F("SpongeBob SquarePants S02E20 Gary Takes a Bath REPACK 1080p AMZN.mkv", 2, 20)]
ok("place: an item fetched AS S02E20 keeps it", not ts.place_files(g, ALLS, (2, 20)))
ts.place_files(g, ALLS, (3, 31))
eq("place: fetched for S03E31, filed there", (g[0]["season"], g[0]["episode"]), (3, 31))

eq("name_query: plain", ts.name_query("Party Pooper Pants"), "Party Pooper Pants")
eq("name_query: punctuation", ts.name_query("Christmas Who?"), "Christmas Who")
eq("name_query: short but usable", ts.name_query("Ugh"), "Ugh")
eq("name_query: placeholder", ts.name_query("Episode 7"), "")
eq("name_query: nothing", ts.name_query(None), "")

print("%d passed, %d failed" % (_PASS, len(_FAIL)))
for f in _FAIL:
    print("  FAIL", f)
sys.exit(1 if _FAIL else 0)
