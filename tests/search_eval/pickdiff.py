"""What 17.3.0 changes about WHICH release gets downloaded, printed side by side.

    python tests/search_eval/pickdiff.py            # all targets
    python tests/search_eval/pickdiff.py "Hunter"   # substring-filter them

**This is a differ, not a scorer, and the distinction is the whole design.** Its
neighbours here (`verify.py`, `score.py`) grade against `labels.json`, which can
exist because "is this release episode 7 of that show" has a right answer a
person can check. "Is this the *better* release" does not. Building ground truth
for it would be seventeen hundred subjective calls encoding one person's taste,
and a number computed from that would look authoritative while meaning nothing.

So this prints the diff and leaves the judgement where it belongs. Two things in
the summary are objective and worth watching:

* **bucket downgrades must be 0.** Richness is capped at the availability bucket
  by construction (see `_pickCmp` in static/index.html), so it can reorder two
  equally-available copies and can never promote a Low one over a Good one. A
  non-zero here is a comparator bug, not a judgement call.
* **seeder drop >10x** is the honest cost line. It is what "richness dragged in
  a worse-seeded release" actually looks like; the titles are printed beside it
  so they can be read rather than summarised.

Mirrors the client-side pickers in static/index.html: `_pickCmp`,
`_ssAutoPickFrom`, `_packCoversScope`, `_bgAbsBatchOk`. When one of those
changes, change it here too, or this stops measuring the shipping code.

Needs a running StreamLink with working indexers (like `verify.py`, unlike the
unit tests next door). Not part of `make test`. Box address below — point it at
yours.
"""
import json, os, ssl, sys, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import reltracks                                                      # noqa: E402

B = "https://192.168.0.106"
CTX = ssl._create_unverified_context()


def fill_tracks(rows):
    """Score `tracks` locally for a box that predates 17.3.0.

    Deliberately the **same** pure function the server runs (`reltracks`, no
    `main` import, no I/O), so this is not a second opinion — it is the shipping
    rule applied to live indexer output. That is what lets the ranking change be
    measured on real data before it is deployed anywhere, rather than after.
    A box already serving `tracks` is left alone.
    """
    local = 0
    for r in rows:
        if r.get("tracks") is None:
            r["tracks"] = reltracks.analyse(r.get("title") or "")[2]
            local += 1
    return local

# (search title, year, season). Chosen to exercise the cases the change is FOR,
# plus two controls that must not move.
TARGETS = [
    ("Hunter x Hunter", 2011, 1),   # the 58/78/12 grid vs TMDb's 62/74/12
    ("Hunter x Hunter", 2011, 2),   # the screenshot: 0 of 74, absolute batches
    ("Hunter x Hunter", 2011, 3),
    ("Oshi no Ko", 2023, 1),        # merged cours - the mirror case
    ("Attack on Titan", 2013, 4),   # two-part season, NOT a grid disagreement
    ("Solo Leveling", 2024, 1),     # the dub-vs-subs case from GOTCHAS
    ("Frieren: Beyond Journey's End", 2023, 1),
    ("Futurama", 1999, 2),          # the broad query misses this season entirely
    ("Hacks", 2021, 4),             # pack relevance
    ("Breaking Bad", 2008, 5),      # CONTROL: English-original, tracks 0, no change
    ("The Bear", 2022, 3),          # CONTROL: ditto
]


def get(path, **q):
    url = "%s%s?%s" % (B, path, urllib.parse.urlencode(q))
    with urllib.request.urlopen(url, context=CTX, timeout=180) as r:
        return json.load(r)


# ── Mirrors of the client pickers ────────────────────────────────────────────

def avail_rank(seeders):
    """_availRank — the ladder the UI has always rendered as Excellent/Good/Low."""
    n = seeders or 0
    return 0 if n <= 0 else 1 if n <= 8 else 2 if n <= 30 else 3


def cmp_old(s):
    """The pre-17.3.0 sort: Dolby-Vision risk last, then raw seeders."""
    return (0 if s.get("dv_risk") else 1, s.get("seeders") or 0)


def cmp_new(s):
    """_pickCmp: dv risk last, availability BUCKET, track richness, exact seeders."""
    return (0 if s.get("dv_risk") else 1, avail_rank(s.get("seeders")),
            s.get("tracks") or 0, s.get("seeders") or 0)


def autopick(sources, key, filt=None):
    """_ssAutoPickFrom's three-tier cascade. No limits set here (the default),
    so tiers 1 and 2 collapse into tier 3 — but the shape is kept so a run with
    limits measures the same thing the app does."""
    filt = filt or {"minSeed": 0, "minBytes": 0, "maxBytes": float("inf")}
    ok_seed = lambda s: (s.get("seeders") or 0) >= filt["minSeed"]
    ok_size = lambda s: filt["minBytes"] <= (s.get("size") or 0) <= filt["maxBytes"]
    for pool in ([s for s in sources if ok_seed(s) and ok_size(s)],
                 [s for s in sources if ok_seed(s)], list(sources)):
        if pool:
            return max(pool, key=key)
    return None


def covers_scope(p, want):
    """_packCoversScope — season / multi-season packs only, absolute batches out."""
    if isinstance(p.get("rel"), (int, float)) and p["rel"] < 0.7:
        return False
    if p.get("ep_from"):
        return False
    if p.get("kind") == "season":
        return p.get("season") == want
    if p.get("kind") == "multiseason":
        fr = p.get("season_from") or 0
        to = p.get("season_to") or fr
        return True if not fr else (fr <= want <= to)
    return False


def abs_span(meta, season):
    """_animeAbsNo's cumulative walk: this TMDb season's absolute (first, last)."""
    grid = sorted(((int(x.get("season") or 0), int(x.get("episode_count") or 0))
                   for x in (meta.get("all_seasons") or [])
                   if int(x.get("season") or 0) > 0 and int(x.get("episode_count") or 0) > 0))
    run = 0
    for n, c in grid:
        if n == season:
            return (run + 1, run + c)
        run += c
    return None


def abs_batch_ok(meta, p, want):
    """_bgAbsBatchOk's CONTAINMENT half. The ownership half needs the library and
    is not modelled here — this run is about what the indexers offer."""
    if not p.get("ep_from"):
        return False
    if isinstance(p.get("rel"), (int, float)) and p["rel"] < 0.7:
        return False
    if not ((meta.get("anime") or {}).get("absolute")):
        return False
    me = abs_span(meta, want)
    if not me:
        return False
    return int(p["ep_from"]) <= me[0] and int(p.get("ep_to") or p["ep_from"]) >= me[1]


def best_pack(packs, key, ok):
    cands = [p for p in packs if ok(p)]
    return max(cands, key=key) if cands else None


def pack_line(p, meta, want):
    if not p:
        return "—"
    extra = ""
    if p.get("ep_from"):
        me = abs_span(meta, want)
        extra = "  abs %s-%s ⊇ %s-%s" % (p["ep_from"], p.get("ep_to") or p["ep_from"],
                                              me[0], me[1]) if me else ""
    return "%s\n%25s seed %-5s %-9s rel %.2f  tracks %d%s" % (
        p["title"][:96], "", p.get("seeders") or 0, p.get("size_human") or "?",
        p.get("rel") or 0, p.get("tracks") or 0, extra)


# ── One target ───────────────────────────────────────────────────────────────

def run(title, year, season):
    meta = get("/api/tmdb/lookup", title=title, year=year, kind="tv")
    q = "%s S%02d" % (title, season)
    data = get("/api/search", q=q, year=year or 0)
    rows = [r for g in (data.get("groups") or []) for r in (g.get("results") or [])]
    if not rows:
        return {"target": "%s S%02d" % (title, season), "empty": True}
    n_local = fill_tracks(rows)

    # `_bgIngest`'s relevance floor (17.3.0). Without it this measures a
    # candidate pool the app no longer has: a "Hunter x Hunter S01" query
    # returns "Interview With The Vampire S01E05" at rel 0.0, and counting a
    # pick that moves off THAT as a win for track richness would be flattering
    # the change with somebody else's bug fix.
    plausible = lambda r: not isinstance(r.get("rel"), (int, float)) or r["rel"] >= 0.7
    eps, packs = [], []
    for r in rows:
        if r.get("kind") == "episode" and r.get("season") == season:
            if plausible(r):
                eps.append(r)
        else:
            packs.append(r)

    out = {"target": "%s (%s)  S%02d" % (title, year, season), "empty": False,
           "n_rows": len(rows), "changed": 0, "n_eps": 0, "downgrade": 0,
           "drop10": [], "gain_aud": 0, "gain_sub": 0, "lines": [],
           "local": n_local}

    p_old = best_pack(packs, lambda p: (p.get("rel") or 0, p.get("seeders") or 0),
                      lambda p: covers_scope(p, season))
    p_new = best_pack(packs, lambda p: (p.get("rel") or 0, p.get("seeders") or 0),
                      lambda p: covers_scope(p, season) or abs_batch_ok(meta, p, season))
    out["gained_pack"] = bool(p_new and not p_old)
    out["gained_abs"] = bool(p_new and not p_old and p_new.get("ep_from"))
    out["lines"].append("  pack (season/multi)  : " + pack_line(p_old, meta, season))
    out["lines"].append("  pack (+abs batch)    : " + pack_line(p_new, meta, season))

    by_ep = {}
    for r in eps:
        by_ep.setdefault(r.get("episode") or 0, []).append(r)
    for ep in sorted(by_ep):
        srcs = by_ep[ep]
        a, b = autopick(srcs, cmp_old), autopick(srcs, cmp_new)
        out["n_eps"] += 1
        if not a or not b or a.get("magnet") == b.get("magnet"):
            continue
        out["changed"] += 1
        if avail_rank(b.get("seeders")) < avail_rank(a.get("seeders")):
            out["downgrade"] += 1
        sa, sb = a.get("seeders") or 0, b.get("seeders") or 0
        if sb and sa > sb * 10:
            out["drop10"].append((out["target"], ep, sa, sb, b["title"][:70]))
        if (b.get("tracks") or 0) & 2 and not ((a.get("tracks") or 0) & 2):
            out["gain_aud"] += 1
        if (b.get("tracks") or 0) & 1 and not ((a.get("tracks") or 0) & 1):
            out["gain_sub"] += 1
        out["lines"].append("  ep S%02dE%02d  old  %-64s seed %-5s tracks %d"
                            % (season, ep, a["title"][:64], sa, a.get("tracks") or 0))
        out["lines"].append("             new  %-64s seed %-5s tracks %d   CHANGED"
                            % (b["title"][:64], sb, b.get("tracks") or 0))
    return out


def main():
    want = (sys.argv[1] or "").lower() if len(sys.argv) > 1 else ""
    targets = [t for t in TARGETS if not want or want in t[0].lower()]
    print("pickdiff — %d targets against %s\n" % (len(targets), B))
    with ThreadPoolExecutor(max_workers=4) as ex:
        results = list(ex.map(lambda t: run(*t), targets))

    tot = {"changed": 0, "n_eps": 0, "downgrade": 0, "packs": 0, "abs": 0,
           "gain_aud": 0, "gain_sub": 0}
    drops = []
    for r in results:
        if r.get("empty"):
            print("%s\n  (no results)\n" % r["target"])
            continue
        print(r["target"] + "   %d results%s"
              % (r["n_rows"], "  (tracks scored locally)" if r.get("local") else ""))
        for ln in r["lines"]:
            print(ln)
        print("  changed %d/%d   bucket-downgrades %d   seeder-drop >10x: %d\n"
              % (r["changed"], r["n_eps"], r["downgrade"], len(r["drop10"])))
        tot["changed"] += r["changed"]
        tot["n_eps"] += r["n_eps"]
        tot["downgrade"] += r["downgrade"]
        tot["packs"] += 1 if r["gained_pack"] else 0
        tot["abs"] += 1 if r["gained_abs"] else 0
        tot["gain_aud"] += r["gain_aud"]
        tot["gain_sub"] += r["gain_sub"]
        drops += r["drop10"]

    pct = (100.0 * tot["changed"] / tot["n_eps"]) if tot["n_eps"] else 0.0
    print("=" * 72)
    print("TOTAL   targets %d   gained a pack %d (of which absolute batches %d)"
          % (len(results), tot["packs"], tot["abs"]))
    print("        episode picks changed %d/%d (%.0f%%)" % (tot["changed"], tot["n_eps"], pct))
    print("        bucket downgrades      %6d      <- must be 0" % tot["downgrade"])
    print("        seeder drop >10x       %6d      <- the cost line, read these" % len(drops))
    print("        picks that gained audio%6d" % tot["gain_aud"])
    print("        picks that gained subs %6d" % tot["gain_sub"])
    for t, ep, sa, sb, ttl in drops[:25]:
        print("          %s E%02d  %d -> %d  %s" % (t, ep, sa, sb, ttl))
    if tot["downgrade"]:
        print("\n!! bucket downgrades are non-zero — _pickCmp is mis-wired, not a"
              "\n   judgement call. Fix before reading anything else here.")


if __name__ == "__main__":
    main()
