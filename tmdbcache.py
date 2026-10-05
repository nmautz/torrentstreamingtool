"""On-disk cache of TMDb API responses, with a stale-copy fallback for outages.

Every TMDb call in `main.py` funnels through `_tmdb_get`, and before this module
every one of them went to the network every time. Two problems followed:

* **Wasted calls.** Opening a long show's library page asks for the episode list
  of every season it has: South Park is ~28 requests, and closing and reopening
  the page after a restart asked for all of them again.
* **Missing metadata offline.** The per-item cache in `library.json` only holds
  the seasons that item's files are in. Everything else (the episode lists of
  seasons you own nothing from, Search, Explore, the show page) came live
  from TMDb, so an internet outage blanked it all even when the same data had
  been fetched an hour earlier.

This cache sits underneath `_tmdb_get`. A response is stored once it arrives. It
is served straight from disk while it is **fresh** (the TTL depends on what kind
of data it is, see `ttl_for`), and when a refetch fails it is served again
**stale**, because the alternative is a page with no episode names at all.

Stale has a ceiling. TMDb's API terms (section 1.C) forbid caching anything
obtained from the API for longer than 6 months, so nothing older than `MAX_AGE`
is ever served or kept: not a response here, not an image in the artwork
cache, not the per-item metadata in `library.json`. The retention rules for all
three live at the bottom of this module (`age_state`, `expire_metadata`) and
`main.py` applies them (`tmdb_retention_loop`). See docs/EXTERNAL_SERVICES.md.

Layout: `<root>/<k[:2]>/<k>.json`, where `k` is a SHA-1 of the path plus the
sorted query params. The API key is excluded, so it never lands on disk and
rotating it doesn't orphan the cache. Writes are atomic (`os.replace`) and every
filesystem error is swallowed. A cache that can't be written is just a slower
TMDb, never a failure.

Stdlib only, no `main` import, like `episodes.py` / `relquality.py`. The clock is
passed in (`now=`) wherever a decision depends on it, so the TTL policy is
unit-testable. See tests/test_tmdbcache.py and docs/LIBRARY_DATA.md § TMDb
response cache.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

HOUR = 3600
DAY = 24 * HOUR

# TMDb API terms, section 1.C: no caching "for longer than 6 months". 180 days
# is under every reading of six months (the shortest run of six is 181).
MAX_AGE = 180 * DAY
# When a copy is this old, ask TMDb for a new one. The 30 days between the two
# are the room an outage has before anything is dropped.
REFRESH_AFTER = 150 * DAY

# Never part of a cache key, and never written to disk.
_SECRET_PARAMS = {"api_key"}

_SEASON_RE = re.compile(r"^/tv/\d+/season/\d+$")
_TV_RE = re.compile(r"^/tv/\d+$")
_MOVIE_RE = re.compile(r"^/movie/\d+$")
_COLLECTION_RE = re.compile(r"^/collection/\d+$")
_EP_GROUPS_RE = re.compile(r"^/tv/\d+/episode_groups$")
_EP_GROUP_RE = re.compile(r"^/tv/episode_group/[0-9a-f]+$")
# Curated, fast-moving lists (Explore rails).
_LIST_RE = re.compile(r"^/(?:trending/|discover/|(?:tv|movie)/(?:popular|top_rated|"
                      r"on_the_air|airing_today|now_playing|upcoming)$)")


def cache_key(path: str, params: Optional[dict] = None) -> str:
    """Stable key for one request: path plus sorted params, minus secrets."""
    q = sorted((str(k), str(v)) for k, v in (params or {}).items()
               if k not in _SECRET_PARAMS)
    raw = path + "?" + "&".join(f"{k}={v}" for k, v in q)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _parse_day(s: str) -> Optional[date]:
    try:
        return date.fromisoformat((s or "")[:10])
    except (TypeError, ValueError):
        return None


def ttl_for(path: str, data: Optional[dict], now: Optional[float] = None) -> float:
    """How long a response stays fresh, in seconds. Shaped by how often TMDb
    actually changes each kind of data:

    * A **season** whose every episode aired more than 60 days ago is settled,
      so it keeps for 30 days. A season still airing (or with undated episodes)
      gets names, stills and air dates filled in week by week, so 12 h.
    * **Show details** carry the season inventory, which is how a new season
      appears in the library at all. 12 h while the show is running, 7 days
      once TMDb calls it Ended/Canceled.
    * **Movie details** carry the theatrical-only flags, so 12 h for anything
      released in the last six months (or not yet), 7 days after that.
    * Episode groups (community arrangements: story arcs, DVD order) are
      edited rarely: a show's list of them 24 h, one group 7 days.
    * Searches, 24 h. Genre lists and collections, 7 days. Curated
      trending/popular lists, 1 h.
    """
    today = date.fromtimestamp(now if now is not None else time.time())
    d = data if isinstance(data, dict) else {}
    if _SEASON_RE.match(path):
        eps = d.get("episodes") or []
        days = [_parse_day(e.get("air_date") or "") for e in eps if isinstance(e, dict)]
        if eps and all(days) and max(days) < today - timedelta(days=60):
            return 30 * DAY
        return 12 * HOUR
    if _TV_RE.match(path):
        return 7 * DAY if d.get("status") in ("Ended", "Canceled") else 12 * HOUR
    if _MOVIE_RE.match(path):
        rel = _parse_day(d.get("release_date") or "")
        if rel and rel < today - timedelta(days=180):
            return 7 * DAY
        return 12 * HOUR
    if _EP_GROUPS_RE.match(path):
        return DAY
    if _EP_GROUP_RE.match(path):
        return 7 * DAY
    if _COLLECTION_RE.match(path) or path.startswith("/genre/"):
        return 7 * DAY
    if path.startswith("/search/"):
        return DAY
    if _LIST_RE.match(path):
        return HOUR
    return 6 * HOUR


class TmdbCache:
    """File-per-response store. Every method is best-effort and never raises."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def _file(self, key: str) -> Path:
        return self.root / key[:2] / f"{key}.json"

    def get(self, path: str, params: Optional[dict] = None,
            now: Optional[float] = None) -> Optional[tuple[dict, bool]]:
        """`(data, fresh)` for a cached response, or None when there's none.
        `fresh` is False once the entry has outlived `ttl_for`. The caller
        should refetch then, but may still serve it if the refetch fails."""
        try:
            with open(self._file(cache_key(path, params)), "r", encoding="utf-8") as f:
                rec = json.load(f)
            data = rec["data"]
            stored = float(rec["stored_at"])
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        t = now if now is not None else time.time()
        if t - stored >= MAX_AGE:
            return None             # past the terms' limit: not even as a fallback
        return data, (t - stored) < ttl_for(path, data, t)

    def put(self, path: str, params: Optional[dict], data: dict,
            now: Optional[float] = None) -> None:
        if not isinstance(data, dict):
            return
        target = self._file(cache_key(path, params))
        rec = {
            "path": path,
            "params": {k: v for k, v in (params or {}).items()
                       if k not in _SECRET_PARAMS},
            "stored_at": now if now is not None else time.time(),
            "data": data,
        }
        tmp = target.with_suffix(f".{os.getpid()}.{id(rec)}.tmp")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(rec, f, separators=(",", ":"))
            # Windows: this fails if a reader has the target open at that exact
            # moment. The next successful fetch writes it instead.
            os.replace(tmp, target)
        except Exception:
            try:
                tmp.unlink()
            except Exception:
                pass

    def prune(self, max_age: float = MAX_AGE, max_entries: int = 20000,
              now: Optional[float] = None) -> int:
        """Drop entries not rewritten within `max_age`, then the oldest beyond
        `max_entries`, plus any orphaned `.tmp` files. Returns how many files
        went. Blocking I/O, so run it off the event loop (`asyncio.to_thread`)."""
        t = now if now is not None else time.time()
        removed = 0
        entries: list[tuple[float, Path]] = []
        try:
            files = list(self.root.glob("*/*"))
        except Exception:
            return 0
        for p in files:
            try:
                mtime = p.stat().st_mtime
                if p.suffix == ".tmp":
                    if t - mtime > HOUR:
                        p.unlink()
                        removed += 1
                    continue
                if t - mtime > max_age:
                    p.unlink()
                    removed += 1
                else:
                    entries.append((mtime, p))
            except Exception:
                pass
        if len(entries) > max_entries:
            entries.sort()
            for _, p in entries[:len(entries) - max_entries]:
                try:
                    p.unlink()
                    removed += 1
                except Exception:
                    pass
        return removed


# ── Retention: nothing from TMDb is kept past six months ─────────────────────
# One rule for the three places TMDb data rests: this response cache, the
# artwork cache, and `item["metadata"]` in library.json. Pure, so the policy is
# testable without a library or a network.

FRESH, REFRESH, EXPIRED = "fresh", "refresh", "expired"

# Section stamps that hold nothing from TMDb: a miss ("none") and a hand-entered
# entry ("custom"). Everything else in `metadata.sections` is TMDb's.
_OWN_SECTION_SOURCES = ("none", "custom")


def age_state(age: Optional[float]) -> str:
    """What to do with a copy `age` seconds old. An unknown age is EXPIRED: a
    copy that can't show it is under six months old doesn't get the benefit of
    the doubt."""
    if age is None or age >= MAX_AGE:
        return EXPIRED
    return REFRESH if age >= REFRESH_AFTER else FRESH


def is_tmdb_metadata(meta) -> bool:
    """Does this `item["metadata"]` hold data fetched from TMDb? A hand-entered
    entry (`source: "custom"`) has no `tmdb_id` and is the user's own."""
    return (isinstance(meta, dict) and meta.get("source") != "custom"
            and bool(meta.get("tmdb_id")))


def metadata_age(meta, now: Optional[float] = None) -> Optional[float]:
    """Seconds since `meta` was fetched (`fetched_at`, ISO 8601), or None when
    it doesn't say or the stamp can't be read."""
    raw = (meta or {}).get("fetched_at") if isinstance(meta, dict) else None
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    t = now if now is not None else time.time()
    return max(0.0, t - dt.timestamp())


def metadata_state(meta, now: Optional[float] = None) -> str:
    """FRESH / REFRESH / EXPIRED for one item's metadata. Metadata that isn't
    TMDb's is always FRESH, and so is a stub `expire_metadata` already emptied
    (there is nothing left in it to expire; `expired` marks it for refetching)."""
    if not is_tmdb_metadata(meta):
        return FRESH if not _has_tmdb_sections(meta) else age_state(metadata_age(meta, now))
    if meta.get("expired"):
        return FRESH
    return age_state(metadata_age(meta, now))


def _has_tmdb_sections(meta) -> bool:
    secs = meta.get("sections") if isinstance(meta, dict) else None
    return isinstance(secs, dict) and any(
        isinstance(b, dict) and b.get("source") not in _OWN_SECTION_SOURCES
        for b in secs.values())


def own_sections(meta) -> dict:
    """The entries of `metadata.sections` that hold nothing from TMDb. These
    survive a refresh and an expiry; the rest are resolved again from scratch."""
    secs = meta.get("sections") if isinstance(meta, dict) else None
    if not isinstance(secs, dict):
        return {}
    return {k: v for k, v in secs.items()
            if isinstance(v, dict) and v.get("source") in _OWN_SECTION_SOURCES}


def expire_metadata(meta) -> Optional[dict]:
    """`meta` with everything TMDb supplied removed, or None when there is
    nothing to remove.

    What stays is ours: WHICH entry the item is bound to (`tmdb_id`,
    `tmdb_kind`) and how that was decided (`source`, so a hand-picked binding is
    still pinned). That is the matching decision, not TMDb's content, and it is
    what lets the item fill back in exactly, with no fuzzy re-match, the moment
    TMDb answers again. `expired: True` marks the stub for that refetch."""
    if not isinstance(meta, dict):
        return None
    if not is_tmdb_metadata(meta):
        # A hand-entered item can still carry TMDb-resolved sections.
        if not _has_tmdb_sections(meta):
            return None
        out = dict(meta)
        out["sections"] = own_sections(meta)
        return out
    if meta.get("expired"):
        return None
    out = {"source": meta.get("source") or "tmdb",
           "tmdb_id": meta["tmdb_id"],
           "tmdb_kind": meta.get("tmdb_kind") or "",
           "expired": True}
    keep = own_sections(meta)
    if keep:
        out["sections"] = keep
    return out
