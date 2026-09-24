"""TMDb episode groups - other ways to arrange a show's episodes.

TMDb's seasons are one arrangement of a show. Its **episode groups** are others,
community-made: story arcs, DVD order, production order, "absolute", a
streaming service's seasons. Attack on Titan has twelve of them.

What makes them cheap to use is that a group never renumbers anything. Every
entry points back at a real TMDb `(season, episode)`, so a group is a VIEW over
the files: they keep their TMDb slots, and progress, prep, downloads and labels
never learn that groups exist. That is the first job of this module
(`summarize`, `normalize`), and the episode page does the rest.

The second job is attribution, because the groups know something TMDb's own
seasons don't. Attack on Titan's two "Final Chapters" specials are TMDb
**S00E36/E37**. TMDb's Final Season stops at episode 28, but every
season-shaped group puts the two specials at its end, and so do the release
groups (`Attack on Titan Season 4/... Finale 1.mkv`). `season_homes` reads
that agreement out of the groups, and `place_files`:

  * stamps `home: {season, after}` on each such special, so the Seasons view
    and the play order (`episodes.sort_key`) show it where it belongs, not
    in a Specials tab after the whole show;
  * places the files that folder read left at `(season, 0)` (there's a season
    and no episode number the parser trusts) onto those specials. That only
    happens when the evidence is unambiguous, and never partially.

Only a bucket that holds exactly one WHOLE TMDb season casts a vote, and one
dissenting group vetoes. Story arcs, DVD discs and anything else that cuts a
season in pieces can't say a special belongs to a season. Measured on the
real data (tests/test_epgroups.py): Attack on Titan's specials land after S04E28,
Firefly's three unaired episodes after S01E11, and Breaking Bad, Game of Thrones
and Hunter x Hunter move nothing.

Leaf module: stdlib + `episodes` (itself a leaf), no `main` import. Plain dicts
in (TMDb's JSON), plain dicts out.
"""

from __future__ import annotations

import os
import re
from typing import Optional

import episodes

# TMDb's `type` codes, as its own site labels them.
TYPE_LABELS: dict[int, str] = {
    1: "Original air date",
    2: "Absolute",
    3: "DVD",
    4: "Digital",
    5: "Story arc",
    6: "Production",
    7: "TV",
}

# Picker order: arrangements a person picks on purpose first, the flat
# "absolute" lists (one bucket of 89) last.
_TYPE_RANK: dict[int, int] = {5: 0, 6: 1, 3: 2, 4: 3, 7: 4, 1: 5, 2: 6}


def _int(v, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def summarize(listing: Optional[dict]) -> list[dict]:
    """The picker's rows, from `/tv/{id}/episode_groups`."""
    out = []
    for g in (listing or {}).get("results") or []:
        if not isinstance(g, dict) or not g.get("id"):
            continue
        t = _int(g.get("type"))
        out.append({
            "id": str(g["id"]),
            "name": str(g.get("name") or "").strip() or TYPE_LABELS.get(t, "Group"),
            "type": t,
            "kind": TYPE_LABELS.get(t, ""),
            "groups": _int(g.get("group_count")),
            "episodes": _int(g.get("episode_count")),
            "description": str(g.get("description") or "").strip(),
        })
    out.sort(key=lambda g: (_TYPE_RANK.get(g["type"], 9), g["name"].lower()))
    return out


def normalize(detail: Optional[dict]) -> Optional[dict]:
    """`/tv/episode_group/{id}` as the page wants it: buckets in their order,
    each bucket's episodes in theirs, and each episode shaped like an entry
    in `metadata.seasons[n].episodes` so the same card renderer draws it."""
    if not isinstance(detail, dict) or not detail.get("id"):
        return None
    buckets = []
    for b in sorted((g for g in detail.get("groups") or [] if isinstance(g, dict)),
                    key=lambda g: _int(g.get("order"))):
        eps = []
        for e in sorted((x for x in b.get("episodes") or [] if isinstance(x, dict)),
                        key=lambda x: _int(x.get("order"))):
            s, n = _int(e.get("season_number"), -1), _int(e.get("episode_number"), -1)
            if s < 0 or n < 0:
                continue
            eps.append({
                "season": s, "episode": n,
                "name": e.get("name") or "",
                "overview": e.get("overview") or "",
                "still_path": e.get("still_path") or "",
                "air_date": e.get("air_date") or "",
                "runtime": e.get("runtime") or 0,
            })
        buckets.append({"name": str(b.get("name") or "").strip(), "episodes": eps})
    t = _int(detail.get("type"))
    return {
        "id": str(detail["id"]),
        "name": str(detail.get("name") or "").strip(),
        "type": t,
        "kind": TYPE_LABELS.get(t, ""),
        "description": str(detail.get("description") or "").strip(),
        "buckets": buckets,
    }


def _season_counts(all_seasons: list) -> dict[int, int]:
    out = {}
    for s in all_seasons or []:
        if isinstance(s, dict):
            n, c = _int(s.get("season"), -1), _int(s.get("episode_count"))
            if n > 0 and c > 0:
                out[n] = c
    return out


def season_homes(groups: list[dict], all_seasons: list) -> dict[int, list[dict]]:
    """Specials that belong inside a numbered season: `{season: [{episode, after,
    name}]}`, each list in viewing order. `groups` are `normalize`d details.

    A vote comes only from a bucket holding exactly one WHOLE TMDb season (every
    episode of it, nothing from another season). A special sitting in such a
    bucket votes for that season, and `after` is the highest season episode
    ahead of it in that bucket (0 = before the first). The highest, not the
    nearest, because an "intended order" group reshuffles the season and
    the Seasons view plays it in TMDb's order. The first group to place a special
    sets its position. A special voted into two different seasons is dropped:
    groups that disagree can't all be right, and "Specials" is the honest
    fallback.
    """
    counts = _season_counts(all_seasons)
    votes: dict[int, set] = {}
    first: dict[int, tuple] = {}         # special -> (after, order-in-bucket, name)
    for g in groups or []:
        for b in (g or {}).get("buckets") or []:
            eps = b.get("episodes") or []
            natives = [(e["season"], e["episode"]) for e in eps if e["season"] > 0]
            seasons = {s for s, _ in natives}
            if len(seasons) != 1:
                continue
            s = next(iter(seasons))
            nums = {n for _, n in natives}
            if not counts.get(s) or nums != set(range(1, counts[s] + 1)):
                continue
            after = 0
            for i, e in enumerate(eps):
                if e["season"] == s:
                    after = max(after, e["episode"])
                elif e["season"] == 0 and e["episode"] > 0:
                    votes.setdefault(e["episode"], set()).add(s)
                    first.setdefault(e["episode"], (after, i, e.get("name") or ""))
    out: dict[int, list[dict]] = {}
    for ep, seasons in votes.items():
        if len(seasons) != 1:
            continue
        after, idx, name = first[ep]
        out.setdefault(next(iter(seasons)), []).append(
            {"episode": ep, "after": after, "name": name, "_i": idx})
    for s in out:
        out[s].sort(key=lambda h: (h["after"], h["_i"]))
        for h in out[s]:
            del h["_i"]
    return out


# ── Placing files ────────────────────────────────────────────────────────────

_TRAIL_NUM_RE = re.compile(r"(?:^|[\s._\-#(])(\d{1,2})\)?\s*$")
_SEASON_TAIL_RE = re.compile(r"(?:season|series|s)\s*\d{1,2}\)?\s*$", re.I)
_WORD_RE = re.compile(r"[a-z]{4,}")
# Words that name the SLOT rather than the episode, so they are no evidence.
_GENERIC = {"season", "series", "episode", "part", "special", "specials", "anime",
            "time", "complete", "collection", "dual", "audio", "bluray", "remux"}


def _stem(f: dict) -> str:
    name = str(f.get("name") or os.path.basename(str(f.get("path") or "")))
    return episodes._clean(os.path.splitext(name)[0])


def trailing_number(f: dict) -> int:
    """The small number a file name ENDS on (`... Finale 2` -> 2), 0 if none.
    A name that ends on its season (`... Season 4`) has no such number."""
    s = _stem(f)
    if _SEASON_TAIL_RE.search(s):
        return 0
    m = _TRAIL_NUM_RE.search(s)
    return int(m.group(1)) if m else 0


def _words(text: str) -> set:
    return {w for w in _WORD_RE.findall((text or "").lower()) if w not in _GENERIC}


def _unplaced(f: dict) -> bool:
    return (_int(f.get("season")) > 0 and _int(f.get("episode")) == 0
            and not f.get("bucket") and not f.get("abs_episode"))


def eligible(files: list[dict]) -> bool:
    """Would group data change anything here? Only an unbucketed special or a
    file stuck at `(season, 0)` can be placed. Everything else (bucketed
    OVAs, whose numbers are local to their folder) is none of this module's
    business. The caller skips the TMDb fetch otherwise."""
    for f in files or []:
        if f.get("bucket"):
            continue
        if _unplaced(f) or (_int(f.get("season")) == 0 and _int(f.get("episode")) > 0):
            return True
    return False


def _match(files: list[dict], cands: list[dict], show_title: str) -> Optional[list]:
    """Pair unplaced files with a season's specials, or None. By number when
    every file ends on a distinct one inside the range (`Finale 1`, `Finale 2`).
    Otherwise by order, when the counts agree and each file shares a real word
    with the special it would become."""
    k = len(cands)
    nums = [trailing_number(f) for f in files]
    if all(1 <= n <= k for n in nums) and len(set(nums)) == len(nums):
        return [(f, cands[n - 1]) for f, n in zip(files, nums)]
    if len(files) != k:
        return None
    show = _words(show_title)
    ordered = sorted(files, key=lambda f: str(f.get("name") or ""))
    pairs = list(zip(ordered, cands))
    for f, c in pairs:
        if not ((_words(_stem(f)) - show) & _words(c.get("name") or "")):
            return None
    return pairs


def place_files(files: list[dict], homes: dict, show_title: str = "") -> bool:
    """Apply `season_homes` to an item's files, in place. True if anything
    changed. Idempotent: its own output (a special at `(0, n)` with a
    `home`) reads back as the same answer.
    """
    changed = False
    homes = {int(s): list(v or []) for s, v in (homes or {}).items()}
    held = {_int(f.get("episode")) for f in files
            if not f.get("bucket") and _int(f.get("season")) == 0}

    for s, cands in homes.items():
        stuck = [f for f in files if _unplaced(f) and _int(f.get("season")) == s]
        if not stuck or not cands:
            continue
        pairs = _match(stuck, cands, show_title)
        # All-or-nothing, and never onto a special some other file already is.
        if not pairs or any(c["episode"] in held for _, c in pairs):
            continue
        for f, c in pairs:
            f["season"], f["episode"] = 0, c["episode"]
            f["home"] = {"season": s, "after": c["after"], "placed": True}
            held.add(c["episode"])
            changed = True

    where = {h["episode"]: (s, h["after"]) for s, hs in homes.items() for h in hs}
    for f in files:
        if f.get("bucket") or _int(f.get("season")) != 0:
            continue
        spot = where.get(_int(f.get("episode")))
        cur = f.get("home")
        if spot:
            want = {"season": spot[0], "after": spot[1]}
            if isinstance(cur, dict) and cur.get("placed"):
                want["placed"] = True
            if cur != want:
                f["home"] = want
                changed = True
        elif isinstance(cur, dict) and not cur.get("placed"):
            # The groups stopped agreeing on it. A file this pass MOVED keeps
            # its home until a reset, so a thinner cache can't make it flicker.
            del f["home"]
            changed = True
    return changed


def reset_files(files: list[dict]) -> bool:
    """Undo `place_files`: a moved file goes back to the `(season, 0)` its
    folder gave it, and every `home` is dropped. The admin Refresh uses this,
    next to `animemap.reset_files`."""
    changed = False
    for f in files or []:
        h = f.pop("home", None)
        if h is None:
            continue
        changed = True
        if isinstance(h, dict) and h.get("placed"):
            f["season"], f["episode"] = _int(h.get("season")), 0
    return changed
