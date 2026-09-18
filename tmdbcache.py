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
**at any age**. Stale data is always better than none, because the alternative is
a page with no episode names at all.

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
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

HOUR = 3600
DAY = 24 * HOUR

# Never part of a cache key, and never written to disk.
_SECRET_PARAMS = {"api_key"}

_SEASON_RE = re.compile(r"^/tv/\d+/season/\d+$")
_TV_RE = re.compile(r"^/tv/\d+$")
_MOVIE_RE = re.compile(r"^/movie/\d+$")
_COLLECTION_RE = re.compile(r"^/collection/\d+$")
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

    def prune(self, max_age: float = 180 * DAY, max_entries: int = 20000,
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
