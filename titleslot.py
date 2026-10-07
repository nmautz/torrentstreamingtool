"""Which episode a release IS, going by the episode name(s) it states.

A release's number is its group's idea of the show, not TMDb's. SpongeBob is
the case that forced this: TMDb lists the 11-minute segments as episodes and
puts "Christmas Who?" at S02E09; the per-segment AMZN releases leave it out, so
"S02E09 Dying for Pie" is TMDb's **E10** and every number after it is one off;
and the half-hour releases (SKST, MeGusta) put two segments in one file -
"S02E09 Survival of the Idiots & Dumped" is TMDb's **E14 and E15**. Any cartoon
made of segments has the same split.

`slot` answers one question for one release or file name: given the season's
TMDb episode names, which episode(s) does it hold? It only ever has an opinion
when the name says so in words:

* An episode's **whole name** must appear in the stated title, as a phrase in
  order, with at least one word of four letters or more ("Day" alone must not
  make "Opposite Day" read as "Valentine's Day").
* A file is **moved** off its own number only when its stated title shares no
  word with TMDb's name for that number. A title that merely differs (a
  translation, an alternate title) names no other episode and stays put.
* A file that names its own episode **and** others keeps its number and gains
  the others in `also`: the second half of a two-segment file.

Everything is derived from the name alone, so the answer is the same every time
it is asked and survives the file list being rebuilt (`build_file_list` runs on
every download-monitor tick). No answer is ever a guess: when the names do not
settle it, `slot` returns None and nothing moves.

`place_files` is attribution **pass 5** in `main._reattribute_item_files`.
`static/index.html` carries a mirror (`_titleSlot`) for search results -
change one, change both.

Leaf module: stdlib only, no `main` import. Tests in `tests/test_titleslot.py`.
See docs/LIBRARY_DATA.md § Season/episode attribution and docs/GOTCHAS.md
§ A release's number is not its episode.
"""

from __future__ import annotations

import html
import re
from typing import Optional

_SXXEXX_RE = re.compile(r"[Ss](\d{1,2})[Ee](\d{1,3})(?!\d)")
_RANGE_TAIL_RE = re.compile(r"^(?:[-_ ]?[Ee](\d{1,3})(?!\d))+|^-(\d{1,3})(?![\dA-Za-z])")

# The first of these ends the episode title: everything after it is the
# release's description of itself. Same list as `_SS_EPTAG` in the dashboard.
_TAG_RE = re.compile(
    r"^(\d{3,4}p|2160p|4k|uhd|web|webdl|web-dl|webrip|hdtv|pdtv|bluray|blu-ray|"
    r"bdrip|brrip|dvdrip|remux|x26[45]|h ?26[45]|26[45]|hevc|avc|xvid|divx|av1|aac.*|ac3|"
    r"dts.*|ddp?\d?|dd|flac.*|opus|atmos|repack\d?|proper|internal|uncut|extended|"
    r"ws|hr|dubbed|subbed|multi.*|vostfr|swesub|subfrench|french|truefrench|german|"
    r"spanish|ita|eng|spa|jap|jpn|nl|nf|amzn|hulu|pcok|dsnp|atvp|hmax|max|cr|all4|"
    r"ip|bbc|itv|iqy|viu|shahid|mp4|mkv|avi|ts|e\d{1,3}|v\d|s\d{1,2}|part|cour|"
    r"batch|complete)$",
    re.IGNORECASE,
)

# Words that carry no identity: joiners between two names, and the scaffolding
# TMDb and releases disagree about ("Mermaid Man & Barnacle Boy" / "... and ...").
_DROP = frozenset({"the", "a", "an", "of", "to", "and", "in", "on", "at", "for",
                   "with", "from", "episode", "chapter", "ep", "pt", "amp"})

# A stated title longer than this is not a title. Two long segment names run to
# about ten words; a third segment is the most any release packs in.
_MAX_WORDS = 18
# How many episodes one file may hold.
_MAX_HELD = 4


def _norm(word: str) -> str:
    """One comparable word: lower case, and a plural reads as its singular so
    "Jellyfish Hunters" is "Jellyfish Hunter" and "Grandmas" is "Grandma's"."""
    w = word.lower()
    return w[:-1] if len(w) >= 4 and w.endswith("s") else w


def words(text) -> list:
    """The identity-carrying words of a title, in order."""
    t = html.unescape(text if isinstance(text, str) else "").lower()
    t = re.sub(r"['’`]", "", t)
    return [_norm(w) for w in re.split(r"[^a-z0-9]+", t) if w and w not in _DROP]


def marker(name: str) -> Optional[tuple]:
    """`(season, episode, rest)` from the first SxxExx in `name`, or None.
    `rest` is what follows the marker and any "-E02" range tail."""
    base = re.split(r"[\\/]", name or "")[-1]
    m = _SXXEXX_RE.search(base)
    if not m:
        return None
    rest = base[m.end():]
    r = _RANGE_TAIL_RE.match(rest)
    if r:
        rest = rest[r.end():]
    return int(m.group(1)), int(m.group(2)), rest


def stated(name: str) -> list:
    """The words of the episode title a release states after its SxxExx marker,
    up to its first release tag. Empty when it states none."""
    mk = marker(name)
    if not mk:
        return []
    out = []
    for raw in re.split(r"[ ._\[\](){}]+", html.unescape(mk[2])):
        if not raw:
            continue
        # "264-Kitsune" and "WEB-DL" are tags; "Spider-Man" is not.
        if _TAG_RE.match(raw) or _TAG_RE.match(raw.split("-")[0]):
            break
        out.extend(words(raw))
        if len(out) > _MAX_WORDS:
            return []
    return out


def _key(name) -> list:
    """An episode name as a matchable phrase, or [] when it cannot identify
    anything: no word of four letters, or a TMDb placeholder ("Episode 7")."""
    k = words(name)
    if not any(len(w) >= 4 and not w.isdigit() for w in k):
        return []
    return k


def _find(needle: list, hay: list) -> list:
    """Every `(start, end)` word range of `hay` that spells `needle`.

    Compared with the spaces taken out, so "Pre-Hibernation Week" is
    "Prehibernation Week" - but a match still has to start and end on a word
    boundary of the release's title."""
    want = "".join(needle)
    out = []
    for i in range(len(hay)):
        got = ""
        for j in range(i, len(hay)):
            got += hay[j]
            if len(got) >= len(want):
                if got == want:
                    out.append((i, j + 1))
                break
    return out


def named(title_words: list, season_eps) -> list:
    """Every episode of the season whose whole name appears in `title_words`,
    as `(position, episode)` in the order stated.

    A name inside a longer matched name is not a second episode ("Shanghaied"
    inside a hypothetical "Shanghaied Again"). Two episodes with the same name
    cannot be told apart, so neither is reported.
    """
    if not title_words:
        return []
    keys = {}
    for e in season_eps or []:
        try:
            no = int(e.get("episode") or 0)
        except (TypeError, ValueError, AttributeError):
            continue
        k = _key(e.get("name")) if no > 0 else []
        if k:
            keys.setdefault(tuple(k), []).append(no)
    spans = []
    for k, nos in keys.items():
        if len(nos) != 1:
            continue
        for a, b in _find(list(k), title_words):
            spans.append((a, b, nos[0]))
    out = []
    for a, b, no in spans:
        if any((a2 <= a and b <= b2) and (b2 - a2) > (b - a) for a2, b2, _ in spans):
            continue
        out.append((a, no))
    out.sort()
    seen, uniq = set(), []
    for pos, no in out:
        if no not in seen:
            seen.add(no)
            uniq.append((pos, no))
    return uniq


def slot(name: str, season_eps) -> Optional[dict]:
    """Where a release or file named `name` belongs, or None for no opinion.

    `season_eps` is TMDb's episode list for the season the name's own marker
    states (`[{episode, name}, ...]`). Returns `{"episode": n, "also": [...]}`:
    the episode it should be filed as and any further episodes it also holds.
    None means leave it where its number puts it.
    """
    mk = marker(name)
    if not mk or mk[0] <= 0 or mk[1] <= 0:
        return None
    own = mk[1]
    title = stated(name)
    hits = [no for _, no in named(title, season_eps)]
    if not hits or len(hits) > _MAX_HELD:
        return None
    first = min(hits)
    if own in hits:
        # It is what its number says, and possibly more: filed under the
        # earliest episode it holds, like every other two-segment file.
        if len(hits) == 1:
            return None
        return {"episode": first, "also": sorted(n for n in hits if n != first)}
    # It names other episodes and not its own. Only a real contradiction moves
    # it: sharing any word with its own number's name means the release may
    # simply call that episode something slightly different.
    own_name = next((e.get("name") for e in season_eps or []
                     if isinstance(e, dict) and _int(e.get("episode")) == own), None)
    own_words = set(w for w in words(own_name) if not w.isdigit())
    if own_words and own_words & set(title):
        return None
    return {"episode": first, "also": sorted(n for n in hits if n != first)}


def _int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def held(f: dict) -> list:
    """Every episode number of its season a library file holds: its own, plus
    `also`. The one question coverage, pack slices and labels all ask."""
    ep = _int(f.get("episode"))
    out = [ep] if ep > 0 else []
    for n in f.get("also") or []:
        n = _int(n)
        if n > 0 and n not in out:
            out.append(n)
    return out


def place_files(files: list, seasons: dict) -> bool:
    """Pass 5: file each episode under the name it states. In place; returns
    True if anything changed.

    `seasons` is `metadata["seasons"]` (`{"2": {"episodes": [...]}}`). Only a
    plain numbered file takes part: not a bucketed one, not one the anime
    passes numbered (`abs_episode` / `abs_no` / `rel_season`), not a special
    with a `home`. And only one whose slot is still what its own name says, or
    one this pass moved before (`ts_from`) - a file something else placed is
    not this pass's to touch.

    A file that loses its reason (TMDb renamed an episode) goes back to its own
    number: the move is re-derived every time, never remembered.
    """
    changed = False
    seasons = seasons if isinstance(seasons, dict) else {}
    for f in files or []:
        if (f.get("bucket") or f.get("abs_episode") or f.get("abs_no")
                or f.get("rel_season") or isinstance(f.get("home"), dict)):
            continue
        mk = marker(f.get("name") or f.get("path") or "")
        if not mk or mk[0] <= 0 or mk[1] <= 0:
            continue
        season, own = mk[0], mk[1]
        cur_s, cur_e = _int(f.get("season")), _int(f.get("episode"))
        moved_before = _int(f.get("ts_from")) == own
        if cur_s != season or (cur_e != own and not moved_before):
            continue
        eps = ((seasons.get(str(season)) or {}).get("episodes")) or []
        if not eps:
            # No names for this season right now: no opinion, and that includes
            # no opinion on undoing an earlier move.
            continue
        got = slot(f.get("name") or f.get("path") or "", eps) or {"episode": own, "also": []}
        if cur_e != got["episode"]:
            f["episode"] = got["episode"]
            changed = True
        if got["episode"] != own:
            if _int(f.get("ts_from")) != own:
                f["ts_from"] = own
                changed = True
        elif "ts_from" in f:
            del f["ts_from"]
            changed = True
        also = [n for n in got["also"] if n != got["episode"]]
        if also:
            if f.get("also") != also:
                f["also"] = also
                changed = True
        elif "also" in f:
            del f["also"]
            changed = True
    return changed
