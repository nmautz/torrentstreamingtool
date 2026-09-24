"""What to CALL a library file - the one label every surface shows.

A file used to be shown by its name: `Breaking.Bad.S01E03.720p.BluRay.x264-DEMAND.mkv`
in the player bar, on the lock screen, in the Downloads tab and in every toast.
A person does not think of an episode that way. They think "Breaking Bad, season
one, episode three, ...And the Bag's in the River", so that is what the label
says, and the file name is only the LAST resort:

    line1   "Breaking Bad · S01E03"            the show and where the episode sits
    line2   "...And the Bag's in the River"    the episode's own name
    short   "Breaking Bad · S01E03"            one line, where two will not fit

The rules, as the user settled them (18.24.0):

  * Everything known          -> line1 "Show · S01E03", line2 the name.
  * Number but no name        -> "Show · S01E03" alone. The file name never appears.
  * Name but no number        -> line1 "Show", line2 the name; short "Show · Name".
  * Neither                   -> the file name (without its extension). The ONLY case.
  * The show                  -> TMDb's series name, then the library's own.
  * The name                  -> TMDb's episode name, then one cleaned out of the
                                 file name. TMDb's placeholder "Episode 7" is no name.
  * A special (TMDb season 0) -> "Special 5"; a bucket's own word for an OVA/OAD/ONA.
  * One file, two episodes    -> "S01E01-E02", both names joined with " / ".
  * Anime the release numbers
    absolutely                -> "S03E12 (148)" when the mapping table knows 148.
  * A movie                   -> "Heat (1995)", one line.

Leaf module: stdlib + `episodes` (itself a leaf), no `main` import. Plain dicts
in (a library file entry + the item's cached metadata), a plain dict out.
Tests in `tests/test_eplabel.py`.
"""

from __future__ import annotations

import os
import re
from typing import Optional

import episodes

SEP = " · "          # " · " - the one separator every surface uses

# ── Pulling a name out of a file name ───────────────────────────────────────
# Mirrors `parseEpisodeInfo` in static/index.html: whatever follows the SxxExx
# marker, up to the first release tag, is the episode's name
# ("Show.S01E03.Pilot.1080p.mkv" -> "Pilot"). The tag list is longer than the
# JS one on purpose: "REPACK" or "MULTi" is not an episode called that.
_SXXEXX_RE = re.compile(r"[Ss](\d{1,2})[Ee](\d{1,3})(?!\d)")
_RANGE_TAIL_RE = re.compile(r"^(?:[-_ ]?[Ee](\d{1,3})(?!\d))+|^-(\d{1,3})(?![\dA-Za-z])")
_RANGE_STEP_RE = re.compile(r"[Ee](\d{1,3})(?!\d)")
_TAG_RE = re.compile(
    r"\b(?:2160p|1080p|1080i|720p|576p|480p|4K|UHD|HDR10?\+?|DV|DoVi|BluRay|Blu-Ray|BDRip|BRRip|"
    r"REMUX|WEB|WEB-?DL|WEBRip|HDTV|DVDRip|AMZN|NF|DSNP|HMAX|ATVP|HULU|x264|x265|"
    r"HEVC|AVC|H\.?26[45]|10bit|AAC\d?|AC3|EAC3|DDP?\d?|DTS|TrueHD|Atmos|FLAC|Opus|"
    r"REPACK|PROPER|INTERNAL|RERIP|MULTi|DUAL|Dual[\s._-]?Audio|Dubbed|Subbed|"
    r"iNTERNAL|LIMITED|UNCUT|EXTENDED)\b.*",
    re.IGNORECASE,
)
_PLACEHOLDER_RE = re.compile(r"^\s*(?:episode|ep\.?|chapter)\s*#?\s*\d+\s*$", re.IGNORECASE)
_SPECIAL_MARK_RE = re.compile(r"[Ss]0{1,2}[Ee]\d")

# The singular a specials bucket is counted in: "Special 5", "OVA 2".
_BUCKET_UNIT = {"Specials": "Special", "OVA": "OVA", "OAD": "OAD", "ONA": "ONA"}

# How far a multi-episode file may reach. Nobody packs six episodes into one
# file; a larger "end" is a different number that happens to follow the marker.
_MAX_SPAN = 5


def _stem(name: str) -> str:
    base = re.split(r"[\\/]", name or "")[-1]
    return os.path.splitext(base)[0]


def clean_stem(name: str) -> str:
    """The file name as a last-resort label: still the file's own title, minus
    the parts that are never part of it - a leading "[Group]", a trailing
    "[CRC32]", and dots standing in for spaces. "[Anime Time] Creditless Ending
    1" -> "Creditless Ending 1"."""
    stem = _stem(name)
    t = re.sub(r"^\s*(?:\[[^\]]*\]\s*)+", "", stem)
    t = re.sub(r"(?:\s*\[[0-9A-Fa-f]{8}\])+\s*$", "", t)
    if " " not in t:
        t = re.sub(r"[._]+", " ", t)
    return t.strip() or stem


def _real_name(name) -> str:
    """A usable episode name, or "" - TMDb fills unnamed episodes with
    "Episode 7", which says nothing the code does not already say."""
    name = (name or "").strip() if isinstance(name, str) else ""
    return "" if (not name or _PLACEHOLDER_RE.match(name)) else name


def name_from_file(file_name: str) -> str:
    """The episode name a release put after its SxxExx marker, or ""."""
    stem = _stem(file_name)
    m = _SXXEXX_RE.search(stem)
    if not m:
        return ""
    rest = stem[m.end():]
    r = _RANGE_TAIL_RE.match(rest)      # "S01E01-E02.Name" -> skip the "-E02"
    if r:
        rest = rest[r.end():]
    rest = re.sub(r"\[.*?\]|\(.*?\)|\{.*?\}", " ", rest)
    rest = _TAG_RE.sub("", rest)
    rest = re.sub(r"[._\-]+", " ", rest)
    rest = re.sub(r"\s+", " ", rest).strip()
    # Nothing left but a year, a number or a release group's leftovers.
    if not rest or re.fullmatch(r"[\d\s]+", rest) or len(rest) < 2:
        return ""
    return _real_name(rest)


def episode_span(file_name: str, season: int, episode: int) -> int:
    """The LAST episode a multi-episode file holds ("S01E01-E02" -> 2), or the
    file's own episode when it holds one. Only trusted when the file name's own
    marker agrees with the attribution - a file the anime pass moved to another
    slot has a name describing the old one."""
    stem = _stem(file_name)
    m = _SXXEXX_RE.search(stem)
    if not m or int(m.group(1)) != season or int(m.group(2)) != episode:
        return episode
    r = _RANGE_TAIL_RE.match(stem[m.end():])
    if not r:
        return episode
    ends = [int(x) for x in _RANGE_STEP_RE.findall(r.group(0))] or [int(r.group(2) or 0)]
    end = max(ends)
    return end if episode < end <= episode + _MAX_SPAN else episode


def _year(date) -> str:
    date = date if isinstance(date, str) else ""
    return date[:4] if re.match(r"\d{4}", date) else ""


def _film_title(binding: dict) -> str:
    title = (binding.get("title") or "").strip()
    if not title:
        return ""
    y = _year(binding.get("release_date"))
    return f"{title} ({y})" if y else title


def _episode_names(eps, first: int, last: int) -> list:
    by_no = {}
    for e in eps or []:
        try:
            by_no[int(e.get("episode") or 0)] = e
        except (TypeError, ValueError, AttributeError):
            continue
    return [_real_name((by_no.get(n) or {}).get("name")) for n in range(first, last + 1)]


def _join_names(names: list) -> str:
    """Every name in a multi-episode file, or none: "Pilot / " would be worse
    than letting the file name's own title speak."""
    if not names or not all(names):
        return ""
    return " / ".join(names)


def _movie(title: str, file_name: str) -> dict:
    title = title or clean_stem(file_name)
    return {"kind": "movie", "show": "", "code": "", "name": title,
            "line1": title, "line2": "", "short": title}


def compose(show: str, code: str, name: str, file_name: str) -> dict:
    """Lay out the parts under the rules in the module docstring."""
    show, code, name = (show or "").strip(), (code or "").strip(), (name or "").strip()
    if code:
        line1 = f"{show}{SEP}{code}" if show else code
        return {"kind": "episode", "show": show, "code": code, "name": name,
                "line1": line1, "line2": name, "short": line1}
    if name:
        return {"kind": "episode", "show": show, "code": "", "name": name,
                "line1": show or name, "line2": name if show else "",
                "short": f"{show}{SEP}{name}" if show else name}
    stem = clean_stem(file_name)
    return {"kind": "file", "show": show, "code": "", "name": "",
            "line1": stem, "line2": "", "short": stem}


def label_file(f: dict, meta: Optional[dict], fallback_show: str = "") -> dict:
    """The label for one library file entry.

    `f` is the stored file dict (`name`, `path`, `season`, `episode`, `bucket`,
    `abs_no`, `abs_episode`); `meta` the item's cached TMDb metadata (may be
    empty); `fallback_show` the library's own series/title for when TMDb has no
    name.
    """
    meta = meta if isinstance(meta, dict) else {}
    file_name = f.get("name") or _stem(f.get("path", ""))
    path = f.get("path", "")
    season = int(f.get("season") or 0)
    episode = int(f.get("episode") or 0)
    bucket = (f.get("bucket") or "").strip()
    show = (meta.get("title") or "").strip() if meta.get("tmdb_kind") != "movie" else ""
    show = show or (fallback_show or "").strip()
    sections = meta.get("sections") if isinstance(meta.get("sections"), dict) else {}

    if bucket:
        kind = episodes.section_kind(bucket)
        sec = sections.get(episodes.section_key(f)) or {}
        if kind == "movies":
            binding = (sec.get("files") or {}).get(path) or {}
            return _movie(_film_title(binding), file_name) if binding else \
                compose(show, "", name_from_file(file_name), file_name)
        if kind == "extras":
            return compose(show, "", name_from_file(file_name), file_name)
        eps = sec.get("episodes") or []
        name = (_real_name(_episode_names(eps, episode, episode)[0]) if episode else "") \
            or name_from_file(file_name)
        if kind == "specials":
            unit = _BUCKET_UNIT.get(bucket, "Special")
            return compose(show, f"{unit} {episode}" if episode else "", name, file_name)
        # A spin-off folder is its own show with its own flat run.
        spin = (sec.get("title") or "").strip() or bucket
        return compose(spin, f"E{episode:02d}" if episode else "", name, file_name)

    # A film-bound item is a film whatever numbers its file name suggested -
    # "Star.Wars.Episode.1.The.Phantom.Menace" parses as episode 1.
    if meta.get("tmdb_kind") == "movie":
        return _movie(_film_title(meta), file_name)

    seasons = meta.get("seasons") if isinstance(meta.get("seasons"), dict) else {}
    if season > 0 and episode > 0:
        last = episode_span(file_name, season, episode)
        code = f"S{season:02d}E{episode:02d}" + (f"-E{last:02d}" if last > episode else "")
        abs_no = int(f.get("abs_no") or 0)
        if abs_no and (abs_no != episode or season != 1):
            code += f" ({abs_no})"
        eps = ((seasons.get(str(season)) or {}).get("episodes")) or []
        name = _join_names(_episode_names(eps, episode, last)) or name_from_file(file_name)
        return compose(show, code, name, file_name)

    if season == 0 and episode > 0:
        # Season 0 with no bucket is one of two things: a real TMDb special
        # ("S00E05" in the name), or an absolute number the TMDb-aware pass has
        # not placed yet ("[Group] Show - 148.mkv"), which IS the main run.
        # A `home` settles it: TMDb's episode groups placed that special
        # (epgroups.py - Attack on Titan's "Season 4 - Finale 1" is S00E36).
        if isinstance(f.get("home"), dict) or _SPECIAL_MARK_RE.search(_stem(file_name)):
            eps = ((seasons.get("0") or {}).get("episodes")) or []
            name = _episode_names(eps, episode, episode)[0] or name_from_file(file_name)
            return compose(show, f"Special {episode}", name, file_name)
        return compose(show, f"E{episode:02d}", name_from_file(file_name), file_name)

    return compose(show, "", name_from_file(file_name), file_name)
