"""Unit tests for `tmdbcache.py`. Run with plain python, no deps:

    python tests/test_tmdbcache.py      (or `make test`)

Same shape as test_relquality.py: a list of cases and a counter. Uses a temp
directory for the store and a fixed clock for every freshness decision.
"""

import os
import shutil
import sys
import tempfile
import time
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tmdbcache as tc           # noqa: E402

_PASS = 0
_FAIL = []


def eq(name, got, want):
    global _PASS
    if got == want:
        _PASS += 1
    else:
        _FAIL.append("%s\n     got:  %r\n     want: %r" % (name, got, want))


NOW = time.mktime((2026, 9, 17, 12, 0, 0, 0, 0, -1))
TODAY = date(2026, 9, 17)


def day(offset):
    return (TODAY + timedelta(days=offset)).isoformat()


# ── cache_key ────────────────────────────────────────────────────────────────
eq("key ignores api_key",
   tc.cache_key("/tv/1", {"api_key": "a", "x": 1}),
   tc.cache_key("/tv/1", {"api_key": "b", "x": 1}))
eq("key ignores param order",
   tc.cache_key("/search/tv", {"query": "q", "page": 2}),
   tc.cache_key("/search/tv", {"page": 2, "query": "q"}))
eq("key differs by params",
   tc.cache_key("/tv/1", {}) == tc.cache_key("/tv/1", {"append_to_response": "videos"}),
   False)
eq("None params == empty params", tc.cache_key("/tv/1"), tc.cache_key("/tv/1", {}))

# ── ttl_for ──────────────────────────────────────────────────────────────────
old_season = {"episodes": [{"air_date": day(-400)}, {"air_date": day(-393)}]}
airing_season = {"episodes": [{"air_date": day(-10)}, {"air_date": day(4)}]}
undated_season = {"episodes": [{"air_date": day(-400)}, {"air_date": ""}]}
eq("settled season → 30 d", tc.ttl_for("/tv/2190/season/3", old_season, NOW), 30 * tc.DAY)
eq("airing season → 12 h", tc.ttl_for("/tv/2190/season/28", airing_season, NOW), 12 * tc.HOUR)
eq("undated episode → 12 h", tc.ttl_for("/tv/2190/season/28", undated_season, NOW), 12 * tc.HOUR)
eq("episode group list → 24 h", tc.ttl_for("/tv/1429/episode_groups", {}, NOW), tc.DAY)
eq("episode group → 7 d", tc.ttl_for("/tv/episode_group/63469b5dd34eb3007e7bce8a", {}, NOW), 7 * tc.DAY)
eq("empty season → 12 h", tc.ttl_for("/tv/2190/season/29", {}, NOW), 12 * tc.HOUR)
eq("running show → 12 h", tc.ttl_for("/tv/2190", {"status": "Returning Series"}, NOW), 12 * tc.HOUR)
eq("ended show → 7 d", tc.ttl_for("/tv/2190", {"status": "Ended"}, NOW), 7 * tc.DAY)
eq("recent movie → 12 h", tc.ttl_for("/movie/5", {"release_date": day(-30)}, NOW), 12 * tc.HOUR)
eq("old movie → 7 d", tc.ttl_for("/movie/5", {"release_date": day(-900)}, NOW), 7 * tc.DAY)
eq("undated movie → 12 h", tc.ttl_for("/movie/5", {}, NOW), 12 * tc.HOUR)
eq("search → 24 h", tc.ttl_for("/search/tv", {}, NOW), tc.DAY)
eq("genres → 7 d", tc.ttl_for("/genre/tv/list", {}, NOW), 7 * tc.DAY)
eq("collection → 7 d", tc.ttl_for("/collection/10", {}, NOW), 7 * tc.DAY)
eq("trending → 1 h", tc.ttl_for("/trending/all/week", {}, NOW), tc.HOUR)
eq("discover → 1 h", tc.ttl_for("/discover/tv", {}, NOW), tc.HOUR)
eq("popular → 1 h", tc.ttl_for("/tv/popular", {}, NOW), tc.HOUR)
eq("other → 6 h", tc.ttl_for("/configuration", {}, NOW), 6 * tc.HOUR)
eq("None data is fine", tc.ttl_for("/tv/1", None, NOW), 12 * tc.HOUR)

# ── store round-trip ─────────────────────────────────────────────────────────
root = tempfile.mkdtemp(prefix="tmdbcache_test_")
try:
    c = tc.TmdbCache(root)
    eq("miss is None", c.get("/tv/1", {}, now=NOW), None)

    c.put("/tv/1", {"api_key": "SECRET", "append_to_response": "videos"},
          {"status": "Returning Series", "name": "X"}, now=NOW)
    eq("fresh hit", c.get("/tv/1", {"append_to_response": "videos"}, now=NOW + 60),
       ({"status": "Returning Series", "name": "X"}, True))
    eq("stale after TTL, still returned",
       c.get("/tv/1", {"append_to_response": "videos"}, now=NOW + 13 * tc.HOUR),
       ({"status": "Returning Series", "name": "X"}, False))

    leaked = False
    for dirpath, _, names in os.walk(root):
        for n in names:
            with open(os.path.join(dirpath, n), encoding="utf-8") as f:
                if "SECRET" in f.read():
                    leaked = True
    eq("api key never written", leaked, False)

    c.put("/tv/1", {"append_to_response": "videos"}, {"name": "Y"}, now=NOW + 100)
    eq("overwrite", c.get("/tv/1", {"append_to_response": "videos"}, now=NOW + 101)[0],
       {"name": "Y"})
    c.put("/tv/2", {}, ["not", "a", "dict"], now=NOW)
    eq("non-dict not stored", c.get("/tv/2", {}, now=NOW), None)

    # A corrupt file reads as a miss rather than raising.
    k = tc.cache_key("/tv/3", {})
    os.makedirs(os.path.join(root, k[:2]), exist_ok=True)
    with open(os.path.join(root, k[:2], k + ".json"), "w") as f:
        f.write("{not json")
    eq("corrupt file → miss", c.get("/tv/3", {}, now=NOW), None)

    # prune: age-out and count cap (mtime drives it, so set it explicitly).
    for i in range(5):
        c.put(f"/tv/{100 + i}", {}, {"i": i}, now=NOW)
        p = c._file(tc.cache_key(f"/tv/{100 + i}", {}))
        os.utime(p, (NOW - i * tc.DAY, NOW - i * tc.DAY))
    for kk in (tc.cache_key("/tv/1", {"append_to_response": "videos"}), k):
        p = c._file(kk)
        os.utime(p, (NOW - 400 * tc.DAY, NOW - 400 * tc.DAY))
    removed = c.prune(max_age=180 * tc.DAY, max_entries=3, now=NOW)
    eq("prune count", removed, 4)          # 2 aged out + 2 oldest over the cap
    eq("newest kept", c.get("/tv/100", {}, now=NOW) is not None, True)
    eq("oldest capped", c.get("/tv/104", {}, now=NOW), None)

    eq("unwritable root is harmless",
       tc.TmdbCache(os.path.join(root, "\0bad")).get("/tv/1", {}), None)
finally:
    shutil.rmtree(root, ignore_errors=True)

# ── Retention: nothing from TMDb past six months ─────────────────────────────
from datetime import datetime, timezone


def _iso(days_ago):
    return datetime.fromtimestamp(NOW - days_ago * tc.DAY, timezone.utc).isoformat(timespec="seconds")


eq("limit is under six months", tc.MAX_AGE <= 181 * tc.DAY, True)
eq("refresh comes first", tc.REFRESH_AFTER < tc.MAX_AGE, True)
eq("young copy", tc.age_state(10 * tc.DAY), tc.FRESH)
eq("due a refresh", tc.age_state(160 * tc.DAY), tc.REFRESH)
eq("at the limit", tc.age_state(tc.MAX_AGE), tc.EXPIRED)
eq("unknown age is not permission", tc.age_state(None), tc.EXPIRED)

root2 = tempfile.mkdtemp(prefix="tmdbcache_ret_")
try:
    c2 = tc.TmdbCache(root2)
    c2.put("/tv/1", {}, {"status": "Ended"}, now=NOW - 179 * tc.DAY)
    c2.put("/tv/2", {}, {"status": "Ended"}, now=NOW - 181 * tc.DAY)
    eq("stale under the limit still serves", c2.get("/tv/1", {}, now=NOW), ({"status": "Ended"}, False))
    eq("past the limit serves nothing", c2.get("/tv/2", {}, now=NOW), None)
finally:
    shutil.rmtree(root2, ignore_errors=True)

tv = {"source": "manual", "tmdb_id": 30984, "tmdb_kind": "tv", "title": "Bleach",
      "overview": "x", "poster_path": "/p.jpg", "all_seasons": [{"season": 1}],
      "seasons": {"1": {"episodes": [{"name": "A"}]}},
      "sections": {"extras": {"source": "none", "kind": "extras", "title": "Extras"},
                   "movies": {"source": "tmdb", "kind": "movies", "title": "Movies"}},
      "fetched_at": _iso(200)}
custom = {"source": "custom", "tmdb_kind": "tv", "title": "Home video",
          "poster_url": "http://x/p.jpg", "fetched_at": _iso(900)}

eq("tmdb metadata", tc.is_tmdb_metadata(tv), True)
eq("custom is the user's own", tc.is_tmdb_metadata(custom), False)
eq("age read from fetched_at", round(tc.metadata_age(tv, NOW) / tc.DAY), 200)
eq("naive stamp read as UTC", round(tc.metadata_age(
    {"fetched_at": _iso(3).replace("+00:00", "")}, NOW) / tc.DAY), 3)
eq("unreadable stamp", tc.metadata_age({"fetched_at": "soon"}, NOW), None)
eq("old tmdb metadata", tc.metadata_state(tv, NOW), tc.EXPIRED)
eq("due metadata", tc.metadata_state(dict(tv, fetched_at=_iso(155)), NOW), tc.REFRESH)
eq("young metadata", tc.metadata_state(dict(tv, fetched_at=_iso(5)), NOW), tc.FRESH)
eq("no stamp is expired", tc.metadata_state(
    {k: v for k, v in tv.items() if k != "fetched_at"}, NOW), tc.EXPIRED)
eq("custom never expires", tc.metadata_state(custom, NOW), tc.FRESH)
eq("no metadata", tc.metadata_state(None, NOW), tc.FRESH)

stub = tc.expire_metadata(tv)
eq("stub keeps the binding", (stub["tmdb_id"], stub["tmdb_kind"], stub["source"]),
   (30984, "tv", "manual"))
eq("stub is marked", stub["expired"], True)
eq("stub holds none of TMDb's content",
   sorted(stub), ["expired", "sections", "source", "tmdb_id", "tmdb_kind"])
eq("only our own sections survive", stub["sections"],
   {"extras": {"source": "none", "kind": "extras", "title": "Extras"}})
eq("input untouched", tv["title"], "Bleach")
eq("a stub is not expired twice", tc.expire_metadata(stub), None)
eq("a stub has nothing left to expire", tc.metadata_state(stub, NOW), tc.FRESH)
eq("custom is left alone", tc.expire_metadata(custom), None)
eq("nothing to expire", tc.expire_metadata(None), None)

mixed = dict(custom, sections={"spin": {"source": "tmdb", "tmdb_id": 5, "title": "Spin"},
                               "mine": {"source": "custom", "title": "Mine"}})
eq("custom item with tmdb sections ages", tc.metadata_state(mixed, NOW), tc.EXPIRED)
out = tc.expire_metadata(mixed)
eq("its own fields stay", out["title"], "Home video")
eq("its tmdb sections go", out["sections"], {"mine": {"source": "custom", "title": "Mine"}})
eq("then nothing more to do", tc.expire_metadata(out), None)

print("tmdbcache: %d passed, %d failed" % (_PASS, len(_FAIL)))
for f in _FAIL:
    print("  FAIL " + f)
sys.exit(1 if _FAIL else 0)
