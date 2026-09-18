"""Anime season grids: turning a release's `SxxExx` into the right TMDb slot.

TMDb files a long-running anime under whatever season split its contributors
chose, and **no release group uses that split**. Hunter x Hunter (2011) is the
clean example — one 148-episode run, and five live conventions for it:

    TMDb            3 seasons, 62 / 74 / 12          (boundaries at 62 and 136)
    iAHD (BD)       3 seasons, 58 / 78 / 12          (boundaries at 58 and 136)
    ZigZag (NF)     6 seasons, numbered ABSOLUTE     (`S02E27` is episode 27)
    scene (W4F)     one season forever               (`S01E59` is episode 59)
    fansub packs    no seasons at all                (`- 059`, `(01-148)`)

`episodes.py` reads the numbers off the filenames correctly in every one of
those cases. What it cannot know is **which grid they are counted on**, and an
`SxxExx` is authoritative there — so iAHD's `S02E01` (really episode 59) lands
on TMDb's S02E01 (really episode 63) and the whole pack sits four episodes out
of true, with its last four files falling off the end of a 74-episode season.

The same mismatch runs the other way. TMDb folds every cour of 【OSHI NO KO】
into one 35-episode Season 1, so a perfectly ordinary `S02E01` names a season
TMDb hasn't got.

There is no way to infer this from the filenames, and neither AniList nor AniDB
answers it on its own: AniList merges where TMDb splits (one 148-episode entry
for Hunter x Hunter) and splits where TMDb merges (11 / 13 / 11 for OSHI NO KO).
It is a third grid, not an arbiter.

What does answer it is **Anime-Lists' `anime-list-full.xml`** — the community
mapping table behind Sonarr's and Jellyfin's anime handling. Each AniDB entry
carries where it lands on TVDB *and* TMDb, with offsets:

    <anime anidbid="8550" tvdbid="252322" tmdbtv="46298" tmdbseason="a">
      <mapping-list>
        <mapping anidbseason="1" tvdbseason="1" start="1"   end="58"/>
        <mapping anidbseason="1" tvdbseason="2" start="59"  end="136" offset="-58"/>
        <mapping anidbseason="1" tvdbseason="3" start="137" end="148" offset="-136"/>

`tmdbseason="a"` is the load-bearing bit: *this run spans several TMDb seasons,
so the absolute number is the real coordinate*. And the windows it lists —
1-58 / 59-136 / 137-148 — are exactly iAHD's split.

This module is stdlib-only with no `main` import, like `episodes.py` and
`tmdbcache.py`. Network fetching stays in `main.py`; here there is only the file
store (`AnimeMap`), the parse, and the pure decision arithmetic. The clock is
passed in (`now=`) wherever staleness is decided, so the policy is testable.

See docs/LIBRARY_DATA.md § Anime season mapping, docs/GOTCHAS.md, and
tests/test_animemap.py.
"""

from __future__ import annotations

import os
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Iterable, Optional

#: Where the table lives. Plain file over HTTPS — no key, no rate limit, no
#: terms that stop us caching it. The `-full` variant is the one carrying the
#: `tmdbtv` / `tmdbseason` / `tmdboffset` attributes; the `-master` variant is
#: AniDB↔TVDB only and would leave us guessing at the TMDb side.
ANIME_LIST_URL = (
    "https://raw.githubusercontent.com/Anime-Lists/anime-lists/master/anime-list-full.xml"
)

#: Refresh interval. The table changes when a new season is announced or a
#: mapping is corrected — neither is urgent, and a week keeps us off GitHub's
#: raw endpoint. A copy older than this is still *used*; it is just refetched.
REFRESH_AFTER = 7 * 24 * 3600

#: Refuse a download that isn't plausibly this file. ~1.7 MB as of 2026-09.
MIN_BYTES = 200_000
MAX_BYTES = 32 * 1024 * 1024

#: `tmdbseason="a"` — the entry spans every TMDb season rather than sitting in
#: one, i.e. the show is a single absolute run that TMDb has subdivided.
ABSOLUTE = "a"


# ── Parsing ──────────────────────────────────────────────────────────────────

def _int(v, default: Optional[int] = None) -> Optional[int]:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


def parse(raw: bytes) -> dict[int, list[dict]]:
    """`anime-list-full.xml` → `{tmdb_tv_id: [entry, …]}`.

    Only entries naming a TMDb **TV** id are kept — a film's mapping has nothing
    to say about a season grid. Each entry is a plain dict so the result is
    JSON-able and the decoders stay free of ElementTree types.
    """
    out: dict[int, list[dict]] = {}
    root = ET.fromstring(raw)
    for a in root.iter("anime"):
        tmdb_id = _int(a.get("tmdbtv"), 0) or 0
        if tmdb_id <= 0:
            continue
        maps = []
        for m in a.findall("mapping-list/mapping"):
            start, end = _int(m.get("start")), _int(m.get("end"))
            if start is None or end is None or end < start:
                continue            # an episode-by-episode special mapping
            maps.append({
                "tvdb_season": _int(m.get("tvdbseason")),
                "tmdb_season": _int(m.get("tmdbseason")),
                "start": start,
                "end": end,
            })
        maps.sort(key=lambda m: m["start"])
        season = a.get("tmdbseason")
        out.setdefault(tmdb_id, []).append({
            "anidb_id":     _int(a.get("anidbid"), 0) or 0,
            "name":         (a.findtext("name") or "").strip(),
            "type":         (a.get("type") or "").strip(),
            # The release-facing season number: release groups follow TVDB's
            # split far more often than TMDb's, and this is the TVDB season
            # this cour is.
            "grid_season":  _int(a.get("defaulttvdbseason")),
            "tmdb_season":  ABSOLUTE if season == ABSOLUTE else _int(season),
            "tmdb_offset":  _int(a.get("tmdboffset"), 0) or 0,
            "mappings":     maps,
        })
    return out


# ── Reading a show's shape ───────────────────────────────────────────────────

def is_absolute_run(entries: list[dict]) -> bool:
    """True when this show is one continuous run that TMDb has subdivided, so a
    series-absolute number is the only stable coordinate (Hunter x Hunter, One
    Piece). False when TMDb's seasons are real seasons (Code Geass) — including
    the case where TMDb *merged* real cours, which `cour_target` handles."""
    return any(e.get("tmdb_season") == ABSOLUTE for e in entries)


def windows(entries: list[dict]) -> list[dict]:
    """Every `start`/`end` absolute window the table lists, sorted. These are the
    boundaries the release groups cut their season packs on."""
    out: list[dict] = []
    for e in entries:
        out.extend(e.get("mappings") or [])
    return sorted(out, key=lambda m: m["start"])


def window_for(entries: list[dict], grid_season: int) -> Optional[dict]:
    """The absolute window covering the release's own season `grid_season`."""
    for m in windows(entries):
        if m.get("tvdb_season") == grid_season:
            return m
    return None


def cour_target(entries: list[dict], grid_season: int) -> Optional[tuple[int, int]]:
    """`(tmdb_season, offset)` when the release's season `grid_season` is a cour
    TMDb folded into a larger season — OSHI NO KO's S2 is TMDb S1 from episode
    12. Returns None when the grids agree (offset 0 onto the same number is a
    no-op we don't need to take a position on) or the show is an absolute run.
    """
    if is_absolute_run(entries):
        return None
    for e in entries:
        if e.get("grid_season") != grid_season:
            continue
        season = e.get("tmdb_season")
        if season is None or season == ABSOLUTE or season <= 0:
            return None
        return season, int(e.get("tmdb_offset") or 0)
    return None


# ── TMDb's own grid ──────────────────────────────────────────────────────────

def _grid(all_seasons: Iterable[dict]) -> list[tuple[int, int]]:
    """`[(season, episode_count), …]` for the positive seasons, in order. Season
    0 is excluded: specials are not part of the absolute run."""
    out = []
    for s in all_seasons or []:
        n, c = _int(s.get("season"), 0) or 0, _int(s.get("episode_count"), 0) or 0
        if n > 0 and c > 0:
            out.append((n, c))
    out.sort()
    return out


def from_absolute(n: int, all_seasons: Iterable[dict]) -> Optional[tuple[int, int]]:
    """Series-absolute number → `(tmdb season, episode)`, or None when it runs
    off the end of what TMDb lists."""
    run = 0
    for season, count in _grid(all_seasons):
        if n <= run + count:
            return season, n - run
        run += count
    return None


def to_absolute(season: int, episode: int, all_seasons: Iterable[dict]) -> Optional[int]:
    """`(tmdb season, episode)` → series-absolute number. The inverse of
    `from_absolute`, and what a search query for an anime episode wants: no
    indexer has "S01E59", plenty have "059"."""
    run = 0
    for s, count in _grid(all_seasons):
        if s == season:
            return run + episode if 0 < episode <= count else None
        run += count
    return None


def total_episodes(all_seasons: Iterable[dict]) -> int:
    return sum(c for _, c in _grid(all_seasons))


# ── Decoding one pack ────────────────────────────────────────────────────────

def decode_pack(entries: list[dict], all_seasons: Iterable[dict],
                rel_season: int, numbers: list[int]) -> Optional[list[tuple[int, int]]]:
    """Where a release's `S{rel_season}E{n}` files really belong on TMDb's grid.

    `numbers` is the pack's whole sorted episode list, because the pack as a
    whole is what says which reading is right — one filename never can. Returns
    one `(season, episode)` per number, or None to mean "no opinion, leave the
    labels alone".

    Every answer is all-or-nothing: if any file would land outside TMDb's grid
    the whole pack is refused, because a half-remapped season is worse than an
    honestly mislabelled one.
    """
    if not entries or not numbers or rel_season <= 0:
        return None
    nums = sorted(numbers)

    # 1) A cour TMDb folded into a bigger season. The release's numbering is
    #    fine, it just starts further along than the label suggests.
    target = cour_target(entries, rel_season)
    if target:
        season, offset = target
        return _checked([(season, n + offset) for n in nums], all_seasons)

    if not is_absolute_run(entries):
        return None                      # real seasons, agreed on both sides

    # 2) An absolute run. Either the numbers are already absolute and the season
    #    label comes from some grid we don't have (a Netflix six-season split),
    #    or they restart at 1 inside the release's own season.
    win = window_for(entries, rel_season)
    in_window = bool(win) and win["start"] <= nums[0] and nums[-1] <= win["end"]
    if nums[0] != 1 and not in_window:
        # A season-local pack always starts at 1. This one doesn't, and doesn't
        # sit in the window its label claims — so the numbers are the absolute
        # ones and the label is decoration.
        return _checked([_pair(from_absolute(n, all_seasons)) for n in nums],
                        all_seasons)
    if not win:
        return None                      # no window for this season — don't guess
    base = win["start"] - 1
    return _checked([_pair(from_absolute(n + base, all_seasons)) for n in nums],
                    all_seasons)


def _pair(v: Optional[tuple[int, int]]) -> tuple[int, int]:
    return v if v else (0, 0)


def _checked(pairs: list[tuple[int, int]],
             all_seasons: Iterable[dict]) -> Optional[list[tuple[int, int]]]:
    """Accept a decode only if every file lands on a real TMDb episode."""
    counts = dict(_grid(all_seasons))
    for season, episode in pairs:
        if season <= 0 or episode <= 0 or episode > counts.get(season, 0):
            return None
    return pairs


# ── Applying it to an item's slots ───────────────────────────────────────────

def remap_slots(slots: list[dict], all_seasons: Iterable[dict],
                entries: list[dict]) -> bool:
    """Third attribution pass: rewrite `slots` onto TMDb's grid. In place;
    returns True if anything moved.

    Runs after `episodes.attribute_paths` and `episodes.resolve_absolute`, on
    the same slot dicts, and only for a show the mapping table knows. Decided
    per release-season, so one odd file can't split a pack across two readings.

    "Anything moved" includes stamping `abs_no` on files that were already in
    the right place: a show whose release grid happens to agree with TMDb's
    still wants its absolute numbers, because that is what an indexer query for
    a missing anime episode has to be built from.

    Bucketed files (specials, OVAs, a spin-off folder) are never touched — they
    sit outside the numbered run by definition, and the table's season windows
    have nothing to say about them.
    """
    if not entries or not _grid(all_seasons):
        return False
    by_season: dict[int, list[dict]] = {}
    for sl in slots:
        if sl.get("bucket"):
            continue
        season, episode = _int(sl.get("season"), 0) or 0, _int(sl.get("episode"), 0) or 0
        if season > 0 and episode > 0:
            by_season.setdefault(season, []).append(sl)

    changed = False
    for season, group in by_season.items():
        # `abs_no` is the "this season has already been decoded" mark, and it has
        # to be, because the pass is not idempotent without one: re-reading a
        # remapped S1E59-62 + S2E1-74 as if they were still release labels
        # would slide season 2 down by another four. The mark survives in
        # `library.json`, and a `build_file_list` rebuild drops it along with
        # the remapped numbers, so the two always agree.
        if any(sl.get("abs_no") for sl in group):
            continue
        # Pass 2 (`episodes.resolve_absolute`) owns this season: its numbers are
        # already TMDb slots rather than the release's own labels, so decoding
        # them again would shift them a second time — a fansub batch numbered
        # 059-075 came out of pass 2 as S1E59..S2E13 and pass 3 slid it to
        # S2E9. Record the absolute numbers and leave the slots alone; `abs_no`
        # then keeps this season settled across reloads, when the transient
        # `abs`/`abs_resolved` marks are long gone.
        if any(sl.get("abs") or sl.get("abs_resolved") for sl in group):
            for sl in group:
                absolute = to_absolute(int(sl["season"]), int(sl["episode"]), all_seasons)
                if absolute and int(sl.get("abs_no", 0) or 0) != absolute:
                    sl["abs_no"] = absolute
                    changed = True
            continue
        group.sort(key=lambda s: int(s["episode"]))
        nums = [int(s["episode"]) for s in group]
        decoded = decode_pack(entries, all_seasons, season, nums)
        if not decoded:
            continue
        for sl, (ns, ne) in zip(group, decoded):
            absolute = to_absolute(ns, ne, all_seasons)
            if absolute and int(sl.get("abs_no", 0) or 0) != absolute:
                sl["abs_no"] = absolute
                changed = True
            if (ns, ne) != (int(sl["season"]), int(sl["episode"])):
                # Keep what the release itself called this file. Nothing in the
                # decode reads it back — it exists so a mapping correction can
                # be re-applied later (`reset_files`) and so "why is this S1E59
                # when the filename says S02E01" has an answer on the record.
                sl.setdefault("rel_season", int(sl["season"]))
                sl.setdefault("rel_episode", int(sl["episode"]))
                sl["season"], sl["episode"] = ns, ne
                sl["abs"] = False
                changed = True
    return changed


def reset_files(files: list[dict]) -> bool:
    """Undo the anime remap on a library item's files, in place.

    Puts back the season and episode the release stated and drops the marks, so
    the next `remap_slots` decodes from scratch against whatever the mapping
    table says now. This is what the admin Refresh button needs: the pass is
    deliberately one-shot (see the `abs_no` guard above), so without a way to
    rewind it, a corrected mapping upstream could never reach a library item.
    """
    changed = False
    for f in files:
        if "rel_season" in f or "rel_episode" in f:
            f["season"] = int(f.pop("rel_season", f.get("season", 0)) or 0)
            f["episode"] = int(f.pop("rel_episode", f.get("episode", 0)) or 0)
            changed = True
        if f.pop("abs_no", None) is not None:
            changed = True
    return changed


# ── The file store ───────────────────────────────────────────────────────────

class AnimeMap:
    """The cached copy of `anime-list-full.xml`, parsed on demand.

    Best-effort throughout, exactly like `tmdbcache.TmdbCache`: a table that
    can't be fetched or can't be written leaves every show reading the way it
    read before this module existed, which is the pre-17.1.0 behaviour and never
    a failure. The parse is memoised on the file's mtime, so the hot attribution
    path costs a `stat()` rather than a 1.7 MB XML parse.
    """

    FILENAME = "anime-list-full.xml"

    def __init__(self, root: Path):
        self.root = Path(root)
        self._index: dict[int, list[dict]] = {}
        self._loaded_mtime: float = -1.0

    @property
    def path(self) -> Path:
        return self.root / self.FILENAME

    def mtime(self) -> Optional[float]:
        try:
            return self.path.stat().st_mtime
        except (OSError, ValueError):
            # ValueError: a root that can't even be a path (embedded NUL). Same
            # answer as a missing file — this store is best-effort throughout.
            return None

    def age(self, now: Optional[float] = None) -> Optional[float]:
        """Seconds since the copy on disk was written, or None if there isn't one."""
        m = self.mtime()
        return None if m is None else max(0.0, (now if now is not None else time.time()) - m)

    def stale(self, now: Optional[float] = None) -> bool:
        """True when it should be refetched — which includes "not there yet"."""
        age = self.age(now)
        return age is None or age >= REFRESH_AFTER

    def store(self, raw: bytes) -> bool:
        """Write a freshly downloaded table, atomically. Refuses anything that
        doesn't parse or is the wrong size — a captive-portal HTML page and a
        truncated download both land here, and overwriting a good table with one
        would silently un-fix every anime on the box."""
        if not raw or not (MIN_BYTES <= len(raw) <= MAX_BYTES):
            return False
        try:
            index = parse(raw)
        except ET.ParseError:
            return False
        if not index:
            return False
        tmp = self.path.with_suffix(f".{os.getpid()}.tmp")
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with open(tmp, "wb") as f:
                f.write(raw)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except (OSError, ValueError):
            try:
                tmp.unlink()
            except (OSError, ValueError):
                pass
            return False
        self._index, self._loaded_mtime = index, self.mtime() or -1.0
        return True

    def index(self) -> dict[int, list[dict]]:
        """The parsed table, `{tmdb_tv_id: [entry, …]}`. `{}` when there is no
        usable copy on disk."""
        m = self.mtime()
        if m is None:
            return {}
        if m != self._loaded_mtime:
            try:
                self._index = parse(self.path.read_bytes())
            except (OSError, ValueError, ET.ParseError):
                self._index = {}
            self._loaded_mtime = m
        return self._index

    def entries_for(self, tmdb_id: int) -> list[dict]:
        """Mapping entries for one TMDb TV id — `[]` for every non-anime show,
        which is what keeps this whole subsystem off the Western-TV path."""
        return self.index().get(int(tmdb_id or 0), [])
