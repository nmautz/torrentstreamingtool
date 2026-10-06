"""Starting a download by voice: which film, and which copy of it (20.6.0).

"Download Star Wars, the original one." Nobody is looking at a screen, so the
two choices a person normally makes on the search page are made here:

  * **Which film was meant** (`parse_request`, `choose_title`). Siri hands over
    the words as spoken. "the original one" / "the new one" / a year say which
    of several films with one name; they are only read off the END of the
    sentence, because "First Blood", "Old" and "The Original Kings of Comedy"
    are titles. The answer is always put back to the person ("Download Star
    Wars (1977)?") before anything starts, so a wrong guess costs a "no".
  * **Which release** (`candidates`, `shortlist`). A port of the dashboard's
    one-press film Get (`grpGetFilm` → `_pickCmp` / `_ssAutoPickRace` in
    static/index.html): the strongest relevance tier only, then Dolby-Vision
    risk last, availability bucket, track richness, seeders. **Change one,
    change both** - a voice download that picks a different copy than the
    button beside the same film would is a bug.

Leaf module: stdlib + `voicestatus`, no `main` import. Tests in
tests/test_voicepick.py. Wired in main.py (`_voice_find`, `_voice_download_film`,
`/api/voice/find`, `/api/voice/download`).
"""

from __future__ import annotations

import re

import voicestatus

_TAIL = r"(?:\s+(?:one|version|movie|film))?\s*$"
_OLDEST = re.compile(r"[,\s]+the\s+(?:original|first|old|older|oldest|classic)" + _TAIL, re.I)
_NEWEST = re.compile(r"[,\s]+the\s+(?:new|newer|newest|latest|recent|remake)" + _TAIL, re.I)
_YEAR = re.compile(r"[,\s]+(?:from\s+|the\s+)?\(?((?:19|20)\d\d)\)?" + _TAIL, re.I)
_LEAD = re.compile(r"^\s*(?:the\s+)?(?:movie|film)\s+", re.I)


def parse_request(spoken: str) -> dict:
    """`{query, year, want}` out of what was said. `want` is "oldest", "newest"
    or ""; `year` is 0 when none was said. A hint is only taken off the end, and
    never when it would leave no title ("1917", "The Remake")."""
    text = re.sub(r"\s+", " ", (spoken or "").strip()).strip(" .?!")
    out = {"query": text, "year": 0, "want": ""}
    lead = _LEAD.sub("", text)
    if lead.strip():
        text = lead
    for _ in range(2):          # "dune the 1984 one", "dune 1984 the original"
        for rx, want in ((_OLDEST, "oldest"), (_NEWEST, "newest")):
            m = rx.search(text)
            if m and text[:m.start()].strip() and not out["want"]:
                out["want"] = want
                text = text[:m.start()]
        m = _YEAR.search(text)
        if m and text[:m.start()].strip() and not out["year"]:
            out["year"] = int(m.group(1))
            text = text[:m.start()]
    out["query"] = text.strip(" ,")
    return out


def _year(c: dict) -> int:
    try:
        return int(str(c.get("year") or "")[:4])
    except ValueError:
        return 0


# A film with under this share of another's TMDb votes is the obscure one of
# the two. Votes, not popularity: popularity is this week's traffic.
OBSCURE = 0.05


def _votes(c: dict) -> int:
    return int(c.get("votes") or 0)


def _known(cands: list[dict]) -> list[dict]:
    """`cands` without the ones nobody has heard of, measured against the best
    known among them. All of them when votes are missing."""
    top = max((_votes(c) for c in cands), default=0)
    return [c for c in cands if _votes(c) >= OBSCURE * top] if top else list(cands)


def choose_title(req: dict, cands: list[dict]) -> "tuple[dict | None, bool]":
    """The candidate meant, and whether its NAME matched what was said.

    `cands` are TMDb results (`title`, `year`, `kind`, `votes`) in TMDb's
    popularity order, which breaks every tie the request does not. A year that
    no candidate has returns None: the caller retries with the words untouched
    ("Blade Runner 2049" is a title, not a film from 2049).

    `sure` False means the name did not settle it and a well-known film TMDb
    returned is offered instead. TMDb searches alternative titles this cannot
    see: "a new hope" finds Star Wars (1977), and matches the NAME of "A
    Christmas in New Hope". Only a word-for-word title outranks fame.
    """
    if not cands:
        return None, False
    scored = [(voicestatus.score(req.get("query", ""), c.get("title", "")), c) for c in cands]
    pool = [(s, c) for s, c in scored if s >= voicestatus.MATCH_MIN]
    year = int(req.get("year") or 0)
    if year:
        dated = [(s, c) for s, c in (pool or scored) if _year(c) == year]
        if not dated:
            return None, False
        return max(dated, key=lambda sc: sc[0])[1], bool(pool)
    famous = max(cands, key=_votes) if any(_votes(c) for c in cands) else cands[0]
    if not pool:
        return famous, False
    top = max(s for s, _ in pool)
    ties = [c for s, c in pool if s >= top - 1e-9]
    if top < 1.0 and max(_votes(c) for c in ties) < OBSCURE * _votes(famous):
        return famous, False
    want = req.get("want") or ""
    dated = [c for c in _known(ties) if _year(c)]
    if want == "oldest" and dated:
        return min(dated, key=_year), True
    if want == "newest" and dated:
        return max(dated, key=_year), True
    return ties[0], True


def series_meant(film: "dict | None", film_sure: bool,
                 show: "dict | None", show_sure: bool) -> bool:
    """Both a film and a series answer to the name: was it the series? Only when
    the film is the obscure one ("The Office" is not the 1966 film; "Fargo" is
    fairly either, and this action downloads films)."""
    if not show or not show_sure:
        return False
    if not film or not film_sure:
        return True
    return _votes(film) < OBSCURE * _votes(show)


# ── which release ────────────────────────────────────────────────────────────

REL_FLOOR = 0.7       # never a candidate below this, however alone it is
REL_BAND = 0.125      # ...and only within this of the best one found
SHORTLIST_MAX = 6


def avail_rank(seeders) -> int:
    """Low / Good / Excellent, the dashboard's `_availRank` ladder."""
    n = int(seeders or 0)
    return 0 if n <= 0 else 1 if n <= 8 else 2 if n <= 30 else 3


def _order_key(r: dict):
    return (1 if r.get("dv_risk") else 0, -avail_rank(r.get("seeders")),
            -int(r.get("tracks") or 0), -int(r.get("seeders") or 0))


def height(title: str) -> int:
    """The resolution a release title claims, or 0 (`_ssRelHeight`)."""
    m = re.search(r"\b(2160|1440|1080|720|576|480)[pi]\b", title or "", re.I)
    if m:
        return int(m.group(1))
    return 2160 if re.search(r"\b(4k|uhd)\b", title or "", re.I) else 0


def release_key(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (title or "").lower())


def _films(results: list[dict]) -> list[dict]:
    return [r for r in results if r.get("magnet") and r.get("kind") != "episode"]


def only_cams(results: list[dict]) -> bool:
    """Every copy found is a cinema recording: the film is not out at home yet,
    which is a different thing to say than "I couldn't find it"."""
    films = _films(results)
    return bool(films) and all(r.get("cam") for r in films)


def candidates(results: list[dict]) -> list[dict]:
    """The releases that may be picked for a film, best first.

    `results` are grouped-search members (`magnet`, `kind`, `rel`, `seeders`,
    `tracks`, `dv_risk`). An episode is never a film; and only the strongest
    relevance tier counts, because every sequel shares the title's words and it
    is the year that tells them apart. A cinema recording (`cam`) is never one:
    it outseeds everything while a film is in cinemas, and nobody asked for it."""
    pool = [r for r in _films(results) if not r.get("cam")]

    def rel(r):
        v = r.get("rel")
        return float(v) if isinstance(v, (int, float)) else 0.0

    best = max((rel(r) for r in pool), default=0.0)
    cut = max(REL_FLOOR, best - REL_BAND)
    return sorted((r for r in pool if rel(r) >= cut), key=_order_key)


def shortlist(ordered: list[dict], ceiling: int = 1080) -> list[dict]:
    """What to race beside the pick, pick first (`_ssAutoPickRace`). The best
    higher-resolution copy at or below `ceiling` goes second, so the race can
    end on a better picture than the fastest start."""
    if not ordered:
        return []
    pick = ordered[0]
    seen, out = set(), []

    def push(r):
        k = release_key(r.get("title", ""))
        if k and k not in seen and r.get("magnet"):
            seen.add(k)
            out.append(r)

    push(pick)
    hq = sorted((r for r in ordered if 0 < height(r.get("title", "")) <= ceiling),
                key=lambda r: (-height(r.get("title", "")),) + _order_key(r))
    if hq and height(hq[0].get("title", "")) > height(pick.get("title", "")):
        push(hq[0])
    for r in ordered:
        push(r)
    # A release reporting no seeders cannot win a race, only take a slot.
    if int(pick.get("seeders") or 0) > 0:
        out = [r for r in out if r is pick or int(r.get("seeders") or 0) > 0]
    return out[:SHORTLIST_MAX]
