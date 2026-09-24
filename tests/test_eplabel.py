"""Unit tests for `eplabel.py` - what a library file is CALLED.

    python tests/test_eplabel.py      (or `make test`)

Each block pins one of the rules the user settled in 18.24.0: the file name is
only ever the label when neither the episode number nor its name is known.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import eplabel as el            # noqa: E402

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


BB = {
    "tmdb_kind": "tv", "title": "Breaking Bad",
    "seasons": {
        "1": {"episodes": [
            {"episode": 1, "name": "Pilot"},
            {"episode": 2, "name": "Cat's in the Bag..."},
            {"episode": 3, "name": "...And the Bag's in the River"},
            {"episode": 4, "name": "Episode 4"},          # TMDb placeholder
        ]},
        "0": {"episodes": [{"episode": 5, "name": "Inside Breaking Bad"}]},
    },
}


def F(name, season=0, episode=0, **kw):
    return {"name": name, "path": "/m/" + name, "season": season, "episode": episode, **kw}


# ── Everything known ─────────────────────────────────────────────────────────
L = el.label_file(F("Breaking.Bad.S01E03.720p.BluRay.x264-DEMAND.mkv", 1, 3), BB, "breaking bad")
eq("full: line1", L["line1"], "Breaking Bad \u00b7 S01E03")
eq("full: line2", L["line2"], "...And the Bag's in the River")
eq("full: short drops the name", L["short"], "Breaking Bad \u00b7 S01E03")
eq("full: kind", L["kind"], "episode")
eq("full: TMDb show name beats the library's", L["show"], "Breaking Bad")

# ── Number but no name: the file name never appears ─────────────────────────
L = el.label_file(F("Breaking.Bad.S01E04.720p.BluRay.x264-DEMAND.mkv", 1, 4), BB, "")
eq("placeholder name counts as none", L["line2"], "")
eq("number only: line1", L["line1"], "Breaking Bad \u00b7 S01E04")
L = el.label_file(F("Breaking.Bad.S01E09.720p.mkv", 1, 9), BB, "")
eq("episode TMDb has not listed", (L["line1"], L["line2"]), ("Breaking Bad \u00b7 S01E09", ""))

# ── Name parsed from the file when TMDb has none ────────────────────────────
L = el.label_file(F("Show.S02E05.The.Long.Night.1080p.WEB-DL.mkv", 2, 5), {}, "Show")
eq("parsed name", L["line2"], "The Long Night")
eq("library title used without TMDb", L["show"], "Show")
eq("parsed: REPACK is not a name", el.name_from_file("Show.S01E01.REPACK.720p.mkv"), "")
eq("parsed: group in brackets dropped", el.name_from_file("[Grp] Show S01E01 - Pilot [1080p].mkv"), "Pilot")
eq("parsed: nothing after marker", el.name_from_file("Show.S01E01.mkv"), "")

# ── Name but no number ───────────────────────────────────────────────────────
L = el.compose("Show", "", "A Name", "x.mkv")
eq("name only: line1 is the show", L["line1"], "Show")
eq("name only: line2 is the name", L["line2"], "A Name")
eq("name only: short joins them", L["short"], "Show \u00b7 A Name")

# ── Neither: the one case the file name is the label ───────────────────────
L = el.label_file(F("some random video.mp4"), {}, "Thing")
eq("fallback: file stem", L["line1"], "some random video")
eq("fallback: kind", L["kind"], "file")
eq("fallback: short", L["short"], "some random video")

# ── Specials ────────────────────────────────────────────────────────────────
L = el.label_file(F("Breaking.Bad.S00E05.Inside.mkv", 0, 5), BB, "")
eq("TMDb special: code", L["code"], "Special 5")
eq("TMDb special: name", L["line2"], "Inside Breaking Bad")
L = el.label_file(F("Show OVA 2.mkv", 0, 2, bucket="OVA"), {"title": "Show", "tmdb_kind": "tv"}, "")
eq("OVA bucket counted in its own word", L["code"], "OVA 2")
L = el.label_file(F("Show - Special 3.mkv", 0, 3, bucket="Specials"), {"title": "Show", "tmdb_kind": "tv"}, "")
eq("Specials bucket", L["line1"], "Show \u00b7 Special 3")
# Unplaced absolute number (no S00 marker) is the main run, not a special.
L = el.label_file(F("[Grp] One Piece - 148.mkv", 0, 148), {"title": "One Piece", "tmdb_kind": "tv"}, "")
eq("unplaced absolute is not a special", L["code"], "E148")

# ── Multi-episode files ─────────────────────────────────────────────────────
L = el.label_file(F("Breaking.Bad.S01E01-E02.720p.mkv", 1, 1), BB, "")
eq("range: code", L["code"], "S01E01-E02")
eq("range: both names", L["line2"], "Pilot / Cat's in the Bag...")
eq("span: S01E01E02", el.episode_span("X.S01E01E02.mkv", 1, 1), 2)
eq("span: S01E01-02", el.episode_span("X.S01E01-02.mkv", 1, 1), 2)
eq("span: 720p is not an episode", el.episode_span("X.S01E01-720p.mkv", 1, 1), 1)
eq("span: 10bit is not an episode", el.episode_span("X.S01E05-10bit.mkv", 1, 5), 5)
eq("span: moved file keeps its slot", el.episode_span("X.S01E01-E02.mkv", 2, 7), 7)
eq("span: implausible reach", el.episode_span("X.S01E01-E40.mkv", 1, 1), 1)
L = el.label_file(F("Breaking.Bad.S01E03-E04.mkv", 1, 3), BB, "")
eq("range with a nameless half shows no names", L["line2"], "")

# ── Anime absolute numbers ──────────────────────────────────────────────────
HXH = {"title": "Hunter x Hunter", "tmdb_kind": "tv",
       "seasons": {"2": {"episodes": [{"episode": 12, "name": "Chimera Ant"}]}}}
L = el.label_file(F("HxH - 074.mkv", 2, 12, abs_no=74), HXH, "")
eq("absolute number rides along", L["code"], "S02E12 (74)")
L = el.label_file(F("HxH - 012.mkv", 1, 12, abs_no=12), HXH, "")
eq("absolute equal to S01 number is not repeated", L["code"], "S01E12")

# ── Movies ──────────────────────────────────────────────────────────────────
HEAT = {"tmdb_kind": "movie", "title": "Heat", "release_date": "1995-12-15"}
L = el.label_file(F("Heat.1995.2160p.UHD.mkv"), HEAT, "Heat")
eq("movie: Title (Year)", (L["line1"], L["line2"], L["kind"]), ("Heat (1995)", "", "movie"))
SHOWM = {"tmdb_kind": "tv", "title": "AoT", "sections": {"movies": {"kind": "movies", "files": {
    "/m/AoT Movie 1.mkv": {"title": "Attack on Titan: Crimson Bow and Arrow", "release_date": "2014-11-22"}}}}}
L = el.label_file(F("AoT Movie 1.mkv", 0, 1, bucket="Movies"), SHOWM, "")
eq("film in a Movies folder", L["line1"], "Attack on Titan: Crimson Bow and Arrow (2014)")

L = el.label_file(F("Star.Wars.Episode.1.The.Phantom.Menace.1999.1080p.mkv", 0, 1),
                  {"tmdb_kind": "movie", "title": "Star Wars: Episode I - The Phantom Menace",
                   "release_date": "1999-05-19"}, "Star Wars Episode 1 1999 1080p")
eq("movie whose name parsed an episode number",
   L["line1"], "Star Wars: Episode I - The Phantom Menace (1999)")

# ── Spin-off folder ─────────────────────────────────────────────────────────
SPIN = {"tmdb_kind": "tv", "title": "AoT", "sections": {"junior high": {
    "kind": "spinoff", "title": "Attack on Titan: Junior High",
    "episodes": [{"episode": 1, "name": "Starting School!"}]}}}
L = el.label_file(F("JH 01.mkv", 0, 1, bucket="Junior High"), SPIN, "")
eq("spin-off: its own show + name",
   (L["line1"], L["line2"]), ("Attack on Titan: Junior High \u00b7 E01", "Starting School!"))

# ── The file name as the label, cleaned ────────────────────────────────────
L = el.label_file(F("[Anime Time] Creditless Ending 1.mkv", 0, 1, bucket="Extras"), {"title": "AoT"}, "")
eq("fallback drops a leading group tag", L["line1"], "Creditless Ending 1")
eq("fallback drops a trailing CRC", el.clean_stem("Some Clip [6CE80996].mkv"), "Some Clip")
eq("fallback turns dots into spaces", el.clean_stem("Deleted.Scenes.mkv"), "Deleted Scenes")
eq("fallback keeps a spaced name as is", el.clean_stem("What Is Chernobyl.mkv"), "What Is Chernobyl")

# ── Nothing at all ──────────────────────────────────────────────────────────
L = el.label_file({"path": "C:\\dl\\x\\clip.avi"}, None, "")
eq("path-only entry, Windows separators", L["line1"], "clip")



# ── A special TMDb's episode groups placed (epgroups.py) ─────────────────────
AOT = {"tmdb_kind": "tv", "title": "Attack on Titan", "seasons": {"0": {"episodes": [
    {"episode": 36, "name": "The Final Chapters Special (1)"}]}}}
L = el.label_file(F("[Anime Time] Attack on Titan Season 4 - Finale 1.mkv", 0, 36,
                    home={"season": 4, "after": 28, "placed": True}), AOT, "")
eq("homed special: code", L["code"], "Special 36")
eq("homed special: TMDb name", L["line2"], "The Final Chapters Special (1)")

print("%d passed, %d failed" % (_PASS, len(_FAIL)))
for f in _FAIL:
    print("  FAIL", f)
sys.exit(1 if _FAIL else 0)
