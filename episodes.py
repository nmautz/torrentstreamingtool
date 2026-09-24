"""Season/episode attribution for library files.

`SxxExx` covers most Western scene releases, but a large slice of what actually
lands on disk — anime batches above all — carries no per-file season marker at
all. The season lives in the *directory* (`…/Attack on Titan Season 2/…`) and the
number in the filename is **series-absolute** (`… - 26.mkv` is season 2 episode
1). Parsing only the basename dumps every such file into `S0E0`, which costs the
library page its season tabs, its TMDb episode names and its stills.

So attribution happens in two passes:

1. **Structural** (`attribute_paths`) — pure, offline, no network. Reads the
   basename first (`SxxExx` / `NxNN` are authoritative), then falls back to a
   season taken from the enclosing directory plus an episode number taken from
   the basename. Files that land outside the numbered run (`Extras`, `OAD`,
   `Movies`, a spin-off folder) are bucketed to season 0 and **labelled** with
   the bucket so the UI can group them instead of dumping them in one list.
   Where the episode number might be series-absolute rather than within-season,
   the slot is flagged `abs`.

2. **TMDb-aware** (`resolve_absolute`) — run once the show's season inventory is
   known. Only `abs` slots are touched. It compares each season's numbers
   against that season's real episode count and, when they instead line up with
   the cumulative absolute window, subtracts the offset.

3. **Anime season grids** (`animemap.remap_slots`, 17.1.0) — the case neither
   pass above can reach: an `SxxExx` that is *authoritative and wrong*, because
   the release counts its seasons on a different grid than TMDb does. It needs
   an external mapping table and so lives in its own module; see `animemap.py`.

Splitting it this way keeps pass 1 usable at download time (when no metadata
exists yet) while pass 2 stays a pure function of `(slots, season counts)`.

See docs/LIBRARY_DATA.md § Season/episode attribution and docs/GOTCHAS.md.
"""

from __future__ import annotations

import os
import re
from typing import Iterable, Optional

# ── Basename patterns ────────────────────────────────────────────────────────
# Authoritative: the file states both numbers itself.
_SXXEXX_RE = re.compile(r"[Ss](\d{1,2})[Ee](\d{1,3})(?!\d)")
_NXNN_RE   = re.compile(r"\b(\d{1,2})x(\d{2})\b")

# Episode-only forms, combined with a season taken from the directory. Ordered
# strongest-first; each is anchored on a marker that can't be a resolution or a
# year. `- 07` is the fansub form and needs the spaces — "H 264-NTb" and
# "x265-RARBG" have no space after the dash, and "Hacks-S05" has none before.
_EP_WORD_RE  = re.compile(r"\b[Ee](?:p|pisode)?[\s._-]*(\d{1,4})\b")
_EP_HASH_RE  = re.compile(r"#\s*(\d{1,4})\b")
_EP_DASH_RE  = re.compile(r"\s-\s+(\d{1,4})(?!\d)")

# Release noise stripped before any episode scan, so a codec/resolution/channel
# tag can never be read as an episode number. Bracketed groups go first: they
# hold the fansub tag ("[Anime Time]"), the tracker stamp ("[eztv.re]") and the
# quality block ("[1080p][HEVC 10bit x265]") — none of which carry the episode.
_BRACKETS_RE = re.compile(r"[\[({][^\[\](){}]*[\])}]")
# A channel layout that is SEPARATED from its codec ("EAC3 2 0", "AAC.5.1",
# "DD+ 7 1"). `_NOISE_RE` below only ever caught the glued spellings
# ("AAC2.0", "DDP5.1"), so a release that spaces or dots the layout apart left
# "2 0" / "5 1" standing in the cleaned stem — and the bare-number fallback
# then read the LAST of those as the episode number.
#
# That is not a theoretical tidy-up. The iVy Futurama packs name their first
# file after the release itself, so it reaches the bare-number scan:
#   "…S03 1080p WEBRip 10bit EAC3 2 0 x265-iVy"  ->  episode 0   (from "2 0")
#   "…S01 1080p WEBRip 10bit EAC3 5 1 x265-iVy"  ->  episode 1   (from "5 1")
# The 5.1 season looked correct purely by coincidence and the 2.0 seasons
# collapsed to episode 0. Anchored on the codec word and limited to real
# layouts (<1-8> <0-1>), so it cannot swallow an episode number.
_AUDIO_CH_RE = re.compile(
    r"\b(?:aac|ac-?3|eac-?3|dd\+?|ddp|dts(?:[\s._-]?hd)?(?:[\s._-]?ma)?|"
    r"truehd|atmos|opus|flac|mp3)[\s._-]+[1-8][\s._-]+[01]\b",
    re.IGNORECASE,
)
_NOISE_RE = re.compile(
    r"\b(?:\d{3,4}[pi]|[xh][\s._-]?26[45]|hevc|avc|av1|xvid|divx|10bit|8bit|"
    r"bluray|blu-ray|bdrip|brrip|webrip|web-?dl|hdtv|dvdrip|remux|"
    r"aac\d?(?:\.\d)?|ac3|eac3|dts(?:-hd)?|ddp?\d(?:\.\d)?|flac|opus|truehd|"
    r"\d+(?:\.\d+)?\s*(?:[gmkt]i?b)|\d+ch|dual[\s._-]?audio|multi|repack|proper)\b",
    re.IGNORECASE,
)
# A bare trailing number is only an episode when the word in front of it isn't
# something else that gets numbered. "Season 4 - Finale 1" must not read as E1.
_NOT_EPISODE_WORDS = {
    "finale", "part", "pt", "volume", "vol", "disc", "disk", "cd", "dvd",
    "season", "series", "chapter", "act", "version", "ver", "v", "cour",
}
# These DO index a file — but only inside a bucket, where the number orders the
# bucket rather than the series. "Season 1/Show Special 1.mkv" must not collide
# with S1E1, while "Specials/Show OVA 2.mkv" is legitimately the second OVA.
_BUCKET_INDEX_WORDS = {
    "special", "specials", "sp", "movie", "movies", "film", "films",
    "ova", "oad", "oav", "ona", "ncop", "nced", "opening", "openings",
    "ending", "endings", "creditless", "extra", "extras", "bonus",
}
_BARE_NUM_RE = re.compile(r"(?:^|[\s._-])(?:(\w+)[\s._-]+)?(\d{1,4})(?!\d)")

# ── Directory patterns ───────────────────────────────────────────────────────
# A multi-season pack folder names a RANGE, never one season — "…(S01-S04+OVA…)"
# is the release root, not season 1. Detect and reject those outright.
_DIR_RANGE_RE = re.compile(
    r"[Ss](?:easons?\s*)?\d{1,2}\s*[-~]\s*[Ss]?\d{1,2}(?!\d)|"
    r"[Ss]\d{1,2}(?:\s*[+&]\s*[Ss]?\d{1,2})+",
)
# "Season 3", "Seasons 3", "Series 3" — the word form, anywhere in the name, so
# "Chernobyl (2019) Season 1 S01 (1080p…)" and "Hacks Season 3 Mp4 1080p" both
# resolve. The bare "S03" form must not swallow "S01E01" (that's a single
# episode, handled by the basename) nor "S05E…"-style folder names.
_DIR_WORD_SEASON_RE = re.compile(r"\b[Ss]e(?:ason|ries)s?\s*(\d{1,2})\b")
_DIR_BARE_SEASON_RE = re.compile(r"\b[Ss](\d{1,2})(?![\dEe])")

# Folders that hold something other than the numbered run. Matched only at the
# END of the component ("Attack On Titan OAD", "Extras") so a release root that
# merely mentions "+OVA+Movies" in its quality blurb is never mistaken for one.
_BUCKET_WORDS: tuple[tuple[str, str], ...] = (
    (r"specials?", "Specials"),
    (r"extras?", "Extras"),
    (r"bonus(?:\s+features?)?", "Extras"),
    (r"movies?|films?", "Movies"),
    (r"ova|oav", "OVA"),
    (r"oad", "OAD"),
    (r"ona", "ONA"),
    (r"nc(?:op|ed)|creditless|openings?|endings?", "Extras"),
)
_BUCKET_RES = tuple(
    (re.compile(rf"(?:^|[\s._\-\](}}])(?:{pat})\s*$", re.IGNORECASE), label)
    for pat, label in _BUCKET_WORDS
)


def _clean(stem: str) -> str:
    """Strip bracketed groups and release noise so only title-ish text and the
    episode number survive."""
    s = _BRACKETS_RE.sub(" ", stem)
    s = _AUDIO_CH_RE.sub(" ", s)      # before _NOISE_RE: it eats the codec word
    s = _NOISE_RE.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def dir_season(component: str) -> Optional[int]:
    """Season number named by one directory component, or None.

    Returns None for a multi-season pack folder and for a folder naming two
    different seasons — both mean "this directory is not one season".
    """
    if not component or _DIR_RANGE_RE.search(component):
        return None
    found = {int(m) for m in _DIR_WORD_SEASON_RE.findall(component)}
    found |= {int(m) for m in _DIR_BARE_SEASON_RE.findall(component)}
    if len(found) != 1:
        return None
    season = found.pop()
    return season if 0 <= season <= 99 else None


def dir_bucket(component: str) -> str:
    """Label for a non-canonical folder ("Specials"/"Extras"/"Movies"/…), or ""."""
    for rx, label in _BUCKET_RES:
        if rx.search(component or ""):
            return label
    return ""


def _episode_from_name(stem: str, allow_bare: bool, in_bucket: bool = False) -> int:
    """Episode number stated by a basename that carries no SxxExx.

    `allow_bare` enables the loose "trailing number" reading — only safe when a
    season was established some other way (i.e. the file sits in a season
    folder), since on its own a bare number is as likely to be a year or a
    release index. `in_bucket` additionally accepts the words that index a
    specials/movies folder (see `_BUCKET_INDEX_WORDS`).
    """
    s = _clean(stem)
    for rx in (_EP_WORD_RE, _EP_HASH_RE, _EP_DASH_RE):
        m = rx.search(s)
        if m:
            return int(m.group(1))
    if not allow_bare:
        return 0
    blocked = _NOT_EPISODE_WORDS if in_bucket else (_NOT_EPISODE_WORDS | _BUCKET_INDEX_WORDS)
    # Last number in the cleaned stem, unless the word in front of it says the
    # number counts something else (a finale part, a volume, a disc…).
    best = 0
    for m in _BARE_NUM_RE.finditer(s):
        prev, num = (m.group(1) or "").lower().strip("._-"), int(m.group(2))
        if prev in blocked:
            continue
        if 1900 <= num <= 2099 and len(m.group(2)) == 4:
            continue                      # a year, not an episode
        best = num
    return best


def _item_root(paths: Iterable[str]) -> str:
    """Deepest directory shared by every path, minus any trailing season/bucket
    component.

    The shared directory is the release folder, whose name is noise. But when an
    item's files all sit in ONE season folder that folder *is* the shared
    directory — stripping it would throw away the only statement of the season,
    so back out of it.
    """
    dirs = [os.path.dirname(p.replace("/", os.sep).replace("\\", os.sep))
            for p in paths]
    dirs = [d for d in dirs if d]
    if not dirs:
        return ""
    try:
        root = os.path.commonpath(dirs) if len(dirs) > 1 else dirs[0]
    except ValueError:                    # different drives — no shared root
        return ""
    while root:
        base = os.path.basename(root)
        if not base or (dir_season(base) is None and not dir_bucket(base)):
            break
        parent = os.path.dirname(root)
        if parent == root:
            break
        root = parent
    return root


def parse_slot(rel_path: str) -> dict:
    """Attribute ONE file, given its path relative to the item root.

    Returns `{"season", "episode", "bucket", "abs"}`:
      * `bucket` — non-empty ⇒ the file is outside the numbered run (specials,
        movies, a spin-off folder); it is the label to group it under.
      * `abs` — the episode number may be series-absolute rather than
        within-season; `resolve_absolute` decides once TMDb counts are known.
    """
    parts = [p for p in re.split(r"[\\/]+", rel_path or "") if p]
    if not parts:
        return {"season": 0, "episode": 0, "bucket": "", "abs": False}
    stem = os.path.splitext(parts[-1])[0]
    dirs = parts[:-1]

    # 1) The basename states both numbers — always authoritative.
    m = _SXXEXX_RE.search(stem) or _NXNN_RE.search(stem)
    if m:
        return {"season": int(m.group(1)), "episode": int(m.group(2)),
                "bucket": "", "abs": False}

    # 2) Nearest enclosing directory that names a season or a bucket. Nearest
    #    wins and the walk stops there, so a release root that mentions other
    #    seasons/buckets in its blurb can't override the real folder.
    season, bucket = 0, ""
    for comp in reversed(dirs):
        s = dir_season(comp)
        if s is not None:
            season = s
            break
        b = dir_bucket(comp)
        if b:
            bucket = b
            break
    else:
        # No season and no known bucket anywhere below the item root. A file
        # sitting in its own subfolder is still SOMETHING separate (a spin-off,
        # a bonus disc) — group it under that folder's name rather than
        # scattering it. A file directly at the root is just an episode.
        if dirs:
            bucket = dirs[-1]

    if bucket:
        # Bucketed files keep their number purely for ordering — it indexes the
        # bucket, not the series, so it must never be remapped as an absolute.
        return {"season": 0, "episode": _episode_from_name(stem, True, in_bucket=True),
                "bucket": bucket, "abs": False}

    if season > 0:
        ep = _episode_from_name(stem, True)
        # Season 1 numbering is identical either way, so there is nothing for
        # the absolute pass to fix there.
        return {"season": season, "episode": ep, "bucket": "",
                "abs": bool(ep) and season > 1}

    # 3) No season anywhere: only a strong marker counts, and whatever it yields
    #    may well be an absolute number ("[Group] One Piece - 1068.mkv").
    ep = _episode_from_name(stem, False)
    return {"season": 0, "episode": ep, "bucket": "", "abs": bool(ep)}


def _fill_season_gaps(slots: list[dict]) -> None:
    """Give a number to the one file in a season that states none.

    Season packs routinely name their first file after the RELEASE instead of
    the episode: `Futurama-1999-S03 1080p WEBRip … x265-iVy.mkv` sits beside
    `Futurama.S03E02…` through `…E15`. The season is known (the folder says
    S03) but the basename carries no episode, so the file lands at episode 0 —
    it sorts to the top of the season as a nameless row, and the TMDb diff then
    reports episode 1 MISSING on a season the box holds complete.

    When a season has exactly ONE such file and the numbered ones leave exactly
    one hole in the run 1..N (N = how many files that season has), the hole is
    the answer and there is nothing to guess.

    Everything less clear-cut is deliberately left alone:
      * two numberless files could go either way round;
      * a season whose siblings are flagged `abs` is series-absolute numbering,
        which belongs to `resolve_absolute` once TMDb counts are known — not to
        an arithmetic guess made before that runs;
      * numbers reaching past N (2, 3, 17) mean this isn't a clean run, so the
        single hole isn't trustworthy.
    """
    by_season: dict[int, list[int]] = {}
    for i, slot in enumerate(slots):
        if slot["bucket"] or slot["season"] <= 0:
            continue
        by_season.setdefault(slot["season"], []).append(i)

    for idxs in by_season.values():
        blanks = [i for i in idxs if slots[i]["episode"] <= 0]
        if len(blanks) != 1:
            continue
        if any(slots[i]["abs"] for i in idxs):
            continue
        nums = {slots[i]["episode"] for i in idxs if slots[i]["episode"] > 0}
        if not nums:
            continue
        holes = [n for n in range(1, len(idxs) + 1) if n not in nums]
        if len(holes) != 1:
            continue
        slots[blanks[0]]["episode"] = holes[0]


def attribute_paths(paths: list[str]) -> list[dict]:
    """Structural attribution for a whole item's file list (pass 1).

    Item-level because the release root has to be identified before a directory
    can be read as meaningful, and that takes every path.
    """
    root = _item_root(paths)
    out = []
    for p in paths:
        norm = p.replace("/", os.sep).replace("\\", os.sep)
        rel = norm
        if root:
            try:
                rel = os.path.relpath(norm, root)
            except ValueError:
                rel = norm
            if rel.startswith(".."):      # outside the shared root — use as-is
                rel = norm
        out.append(parse_slot(rel))
    _fill_season_gaps(out)
    return out


def season_offsets(all_seasons: list[dict]) -> dict[int, int]:
    """Cumulative episode count BEFORE each season — the amount to subtract to
    turn a series-absolute number into a within-season one. Season 0 is excluded
    (specials are not part of the absolute run)."""
    offsets, running = {}, 0
    for s in sorted((x for x in all_seasons or [] if int(x.get("season", 0) or 0) > 0),
                    key=lambda x: int(x["season"])):
        offsets[int(s["season"])] = running
        running += int(s.get("episode_count", 0) or 0)
    return offsets


def resolve_absolute(slots: list[dict], all_seasons: list[dict]) -> bool:
    """Pass 2: rewrite series-absolute numbers to within-season ones in place.

    Returns True if anything changed. Only `abs` slots are ever touched, so a
    number read straight off an `SxxExx` — or corrected by hand — is safe.
    """
    counts = {int(s.get("season", 0) or 0): int(s.get("episode_count", 0) or 0)
              for s in all_seasons or [] if int(s.get("season", 0) or 0) > 0}
    if not counts:
        return False
    offsets = season_offsets(all_seasons)
    changed = False

    # Case A — the season is known (it came from the folder) but the numbers are
    # absolute. Decide per season, not per file: a whole season's run either
    # fits 1..count or it fits the absolute window, and one outlier shouldn't
    # split the season across both readings.
    by_season: dict[int, list[dict]] = {}
    for sl in slots:
        if sl.get("abs") and sl.get("season", 0) > 0 and sl.get("episode", 0) > 0:
            by_season.setdefault(int(sl["season"]), []).append(sl)
    for season, group in by_season.items():
        count, offset = counts.get(season, 0), offsets.get(season, 0)
        if not count or not offset:
            continue
        nums = [int(s["episode"]) for s in group]
        if max(nums) <= count:
            continue                                  # already within-season
        if not all(offset < n <= offset + count for n in nums):
            continue                                  # not the absolute window
        for sl in group:
            sl["episode"] = int(sl["episode"]) - offset
            sl["abs"] = False
            # Transient, for the anime pass that runs straight after: these
            # numbers are now TMDb slots, not the release's own labels, so it
            # must not read them as labels and shift them a second time. Not
            # persisted — `abs_no` is what carries the fact across a reload.
            sl["abs_resolved"] = True
            changed = True

    # Case B — no season anywhere in the item, so every number is absolute and
    # has to be walked against the cumulative counts. Skipped the moment ANY
    # file carries a real season: mixing the two readings inside one item is how
    # a spin-off folder ends up overwriting the main run.
    if not any(int(s.get("season", 0) or 0) > 0 for s in slots):
        for sl in slots:
            if not (sl.get("abs") and sl.get("episode", 0) > 0):
                continue
            n = int(sl["episode"])
            for season in sorted(counts):
                if n <= counts[season] + offsets[season]:
                    sl["season"] = season
                    sl["episode"] = n - offsets[season]
                    sl["abs"] = False
                    sl["abs_resolved"] = True      # see Case A
                    changed = True
                    break
    return changed


def apply_slot(file_dict: dict, slot: dict) -> bool:
    """Write a slot onto a library file dict. Returns True if anything changed.

    `bucket` is only ever stored when non-empty — the overwhelming majority of
    files are plain episodes and don't need the key.
    """
    changed = False
    for key in ("season", "episode"):
        if int(file_dict.get(key, 0) or 0) != int(slot.get(key, 0) or 0):
            file_dict[key] = int(slot.get(key, 0) or 0)
            changed = True
    bucket = slot.get("bucket") or ""
    if bucket:
        if file_dict.get("bucket") != bucket:
            file_dict["bucket"] = bucket
            changed = True
    elif "bucket" in file_dict:
        del file_dict["bucket"]
        changed = True
    if slot.get("abs"):
        if not file_dict.get("abs_episode"):
            file_dict["abs_episode"] = True
            changed = True
    elif "abs_episode" in file_dict:
        del file_dict["abs_episode"]
        changed = True
    # `abs_no` is the series-absolute episode number, written only by the anime
    # pass (animemap.remap_slots) and only for shows it has a mapping for. It is
    # additive: a slot that doesn't carry one never clears one, so the ordinary
    # structural re-runs above can't wipe it.
    abs_no = int(slot.get("abs_no", 0) or 0)
    if abs_no and int(file_dict.get("abs_no", 0) or 0) != abs_no:
        file_dict["abs_no"] = abs_no
        changed = True
    # Likewise `rel_season`/`rel_episode` — what the release itself called this
    # file, kept only where the anime pass moved it (see animemap.reset_files).
    for key in ("rel_season", "rel_episode"):
        val = int(slot.get(key, 0) or 0)
        if val and int(file_dict.get(key, 0) or 0) != val:
            file_dict[key] = val
            changed = True
    return changed


def sort_key(f: dict) -> tuple:
    """Canonical library file order: seasons ascending, season-0 buckets last
    (grouped by label), then episode, then name.

    A special with a `home` (`epgroups.place_files` - TMDb's episode groups
    agree it belongs inside a numbered season) sorts right after the episode it
    follows there, so the play order runs S04E28, then the two Final Chapters
    specials, instead of saving them until after the whole show."""
    home = f.get("home")
    if isinstance(home, dict) and int(home.get("season", 0) or 0) > 0:
        return (int(home["season"]), "", int(home.get("after", 0) or 0), 1,
                int(f.get("episode", 0) or 0), f.get("name") or "")
    season = int(f.get("season", 0) or 0)
    return (season or 9999, f.get("bucket") or "",
            int(f.get("episode", 0) or 0) or 9999, 0, 0, f.get("name") or "")


# ── Sections ─────────────────────────────────────────────────────────────────
# A `bucket` already records that a file sits outside the numbered run, but the
# UI (and, more importantly, *progress*) treated the whole show as one flat
# ordered list: finishing S04 rolled straight on into the creditless openings,
# and a Junior High episode left half-watched became the show's resume point.
#
# A **section** is that bucket promoted to a first-class unit — its own resume
# point, its own watched count, its own metadata binding. The main run is always
# section `main`; every other section is one bucket. Pure and derivable, so no
# migration is needed: the same files produce the same sections on every load.

SECTION_MAIN = "main"

# Generic bucket words name part of the PARENT show (its specials, its films).
# Anything else is a folder someone named after a different title — a spin-off
# with its own TMDb entry, e.g. "Attack On Titan Junior High".
_SECTION_KINDS: dict[str, str] = {
    "Specials": "specials",
    "OVA":      "specials",
    "OAD":      "specials",
    "ONA":      "specials",
    "Movies":   "movies",
    "Extras":   "extras",
}

# Main first, then the parent show's own content (its specials, then its films),
# then a spin-off series, and throwaway Extras last. Drives section ordering
# everywhere — the library tile, the group page, Play All.
_KIND_ORDER: dict[str, int] = {
    "main": 0, "specials": 1, "movies": 2, "spinoff": 3, "extras": 9,
}

#: Sections whose episodes count toward the show's overall watched total and are
#: swept up by a show-level Play All. Creditless openings are not content.
COUNTED_KINDS = frozenset({"main", "specials", "movies", "spinoff"})


def section_kind(bucket: str) -> str:
    """Which kind of section a bucket label denotes. "" (no bucket) is the main
    run — including the season-0-with-no-bucket case that absolute-numbered
    anime lands in, which IS the numbered run and must never be split off."""
    if not bucket:
        return "main"
    return _SECTION_KINDS.get(bucket, "spinoff")


def section_key(f: dict) -> str:
    """Stable per-file section key. Used to scope progress, so it must not drift
    between loads: it is derived from the bucket label the attribution pass
    already persisted, never from anything re-parsed per call."""
    bucket = f.get("bucket") or ""
    return SECTION_MAIN if not bucket else bucket.strip().lower()


def section_label(key: str, bucket: str, show_title: str = "") -> str:
    """Human name for a section. The main run is named after the show when we
    know the show's name, so a group page reads "Attack on Titan / Junior High /
    Movies" rather than "Main Series / Junior High / Movies"."""
    if key == SECTION_MAIN:
        return show_title.strip() or "Main Series"
    return bucket


def sections_for(files: list[dict], show_title: str = "") -> list[dict]:
    """Partition a show's files into ordered sections.

    Returns ``[{key, label, kind, bucket, files, count, counted}]`` with the main
    run first and Extras last. Files inside each section keep canonical
    `sort_key` order, so a section's list is directly playable.
    """
    by_key: dict[str, dict] = {}
    for f in files:
        key = section_key(f)
        bucket = f.get("bucket") or ""
        sec = by_key.get(key)
        if sec is None:
            kind = section_kind(bucket)
            sec = by_key[key] = {
                "key":     key,
                "label":   section_label(key, bucket, show_title),
                "kind":    kind,
                "bucket":  bucket,
                "counted": kind in COUNTED_KINDS,
                "files":   [],
            }
        sec["files"].append(f)

    out = list(by_key.values())
    for sec in out:
        sec["files"].sort(key=sort_key)
        sec["count"] = len(sec["files"])
    out.sort(key=lambda s: (_KIND_ORDER.get(s["kind"], 5), s["label"].lower()))
    return out
