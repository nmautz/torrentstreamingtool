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


# A name this long may be stated with one word missing.
_NEAR_MIN_WORDS = 4


def _near(keys: dict, title_words: list, exact: list) -> list:
    """Spans where a long name appears with ONE word left out: "Mermaid and
    Barnacle Boy V" for "Mermaid Man and Barnacle Boy V".

    Only for a name of four words or more, only when what is left still has a
    word of four letters, never over words an exact match already explains, and
    never when two episodes could both be meant: "Mermaid Man and Barnacle Boy"
    with the numeral dropped is IV as much as it is V, so it is neither."""
    found = {}
    for k, nos in keys.items():
        if len(nos) != 1 or len(k) < _NEAR_MIN_WORDS:
            continue
        for i in range(len(k)):
            short = list(k[:i] + k[i + 1:])
            if not any(len(w) >= 4 and not w.isdigit() for w in short):
                continue
            for a, b in _find(short, title_words):
                found.setdefault((a, b), set()).add(nos[0])
    out = []
    for (a, b), nos in found.items():
        if len(nos) != 1:
            continue
        if any(a < eb and ea < b for ea, eb, _ in exact):
            continue
        out.append((a, b, next(iter(nos))))
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
    spans.extend(_near(keys, title_words, spans))
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


def states(name: str, ep_name) -> bool:
    """Does the release's stated title say `ep_name`?

    The cross-season question: "S00E03 The Sponge Who Could Fly" asked about
    TMDb's S03E30 of that name. A name that can identify an episode counts as
    a phrase anywhere in the title (one word missing allowed for a long one,
    as in `named`). A shorter one - "Ugh" - counts only as the title's LAST
    words ("Spongebob BC Ugh"): too little to find in the middle of anything."""
    title = stated(name)
    if not title:
        return False
    k = _key(ep_name)
    if k:
        if _find(k, title):
            return True
        if len(k) >= _NEAR_MIN_WORDS:
            for i in range(len(k)):
                short = k[:i] + k[i + 1:]
                if any(len(w) >= 4 and not w.isdigit() for w in short) and _find(short, title):
                    return True
        return False
    k = [w for w in words(ep_name)]
    if not any(not w.isdigit() for w in k) or len("".join(k)) < 3:
        return False
    return len(title) >= len(k) and "".join(title[-len(k):]) == "".join(k) \
        and title[-len(k):][0] == k[0]


def name_query(ep_name) -> str:
    """An episode's name as an indexer search term, or "" when it could single
    nothing out (a placeholder like "Episode 7", or under three letters)."""
    k = words(ep_name)
    if not any(not w.isdigit() for w in k) or len("".join(k)) < 3:
        return ""
    return re.sub(r"\s+", " ", re.sub(r"[^0-9A-Za-z' ]+", " ", str(ep_name))).strip()


def at_home(name: str, own_season_eps) -> bool:
    """Is a release where its own number says, as far as its title shows?
    True when the title names an episode of its own season, or shares a word
    with the name of the episode its number points at."""
    mk = marker(name)
    if not mk:
        return True
    title = stated(name)
    if named(title, own_season_eps):
        return True
    own_name = next((e.get("name") for e in own_season_eps or []
                     if isinstance(e, dict) and _int(e.get("episode")) == mk[1]), None)
    own_words = set(w for w in words(own_name) if not w.isdigit())
    return bool(own_words and own_words & set(title))


def cross(name: str, want_season: int, want_episode: int, seasons: dict) -> bool:
    """May a release numbered in ANOTHER season be filed as
    (`want_season`, `want_episode`)?

    Only ever asked about one wanted episode, never scanned for: its stated
    title must say that episode's name (`states`), and it must not be at home
    where it is. A special (season 0) is taken at its word. A numbered season
    must have its names to hand and contradict them - with no names there is
    no evidence it is misplaced, and no evidence is not permission."""
    mk = marker(name)
    if not mk or mk[1] <= 0 or want_season <= 0 or want_episode <= 0 or mk[0] == want_season:
        return False
    seasons = seasons if isinstance(seasons, dict) else {}
    want_eps = ((seasons.get(str(want_season)) or {}).get("episodes")) or []
    want_name = next((e.get("name") for e in want_eps
                      if isinstance(e, dict) and _int(e.get("episode")) == want_episode), None)
    if not want_name or not states(name, want_name):
        return False
    if mk[0] == 0:
        return True
    own_eps = ((seasons.get(str(mk[0])) or {}).get("episodes")) or []
    return bool(own_eps) and not at_home(name, own_eps)


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


def place_files(files: list, seasons: dict, want: Optional[tuple] = None) -> bool:
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

    `want` is the `(season, episode)` the item was downloaded FOR (20.15.0).
    It is the one way a file crosses seasons: "S00E03 The Sponge Who Could
    Fly" fetched for S03E30 is filed there, when `cross` agrees. It also
    records `ts_from_season`.
    """
    changed = False
    seasons = seasons if isinstance(seasons, dict) else {}
    for f in files or []:
        if f.get("bucket") or f.get("abs_episode") or f.get("abs_no") or f.get("rel_season"):
            continue
        mk = marker(f.get("name") or f.get("path") or "")
        if not mk or mk[1] <= 0:
            continue
        season, own = mk[0], mk[1]
        cur_s, cur_e = _int(f.get("season")), _int(f.get("episode"))
        # ── Across seasons, for the episode this item was fetched for ───────
        crossed = "ts_from_season" in f and _int(f.get("ts_from_season")) == season \
            and _int(f.get("ts_from")) == own
        if want and (crossed or (cur_s, cur_e) == (season, own)) \
                and cross(f.get("name") or f.get("path") or "", _int(want[0]), _int(want[1]), seasons):
            if (cur_s, cur_e) != (_int(want[0]), _int(want[1])) or not crossed:
                f["season"], f["episode"] = _int(want[0]), _int(want[1])
                f["ts_from"], f["ts_from_season"] = own, season
                f.pop("home", None)
                f.pop("also", None)
                changed = True
            continue
        if crossed:
            # Moved across seasons before and no longer justified. With the
            # wanted season's names gone that is "no opinion"; otherwise home.
            if want and not ((seasons.get(str(_int(want[0]))) or {}).get("episodes")):
                continue
            f["season"], f["episode"] = season, own
            del f["ts_from"], f["ts_from_season"]
            cur_s, cur_e = season, own
            changed = True
        if isinstance(f.get("home"), dict) or season <= 0:
            continue
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
