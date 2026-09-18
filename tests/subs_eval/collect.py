"""Build the subtitle-search corpus from a LIVE box.

Every target is a library file that has a prepped HLS bundle carrying an English
TEXT subtitle track. That embedded track is the answer key: score.py searches as
if the file had no subs, downloads what comes back, and grades each download
against it (right content? in sync?).

    python tests/subs_eval/collect.py            # writes corpus.json + .cache/refs/

Up to PER_SHOW files per show, spread across the run, so one 150-episode anime
cannot drown the set. IMDb ids come from Wikidata (TMDb id -> IMDb id, no key),
because the legacy OpenSubtitles API only searches by IMDb and the box never
exposes its TMDb key.
"""
import json, re, ssl, sys, time, urllib.parse, urllib.request
from pathlib import Path

B = "https://192.168.0.106"
ADMIN_PW = "FantaFan43"
PER_SHOW = 5
HERE = Path(__file__).parent
CACHE = HERE / ".cache"
REFS = CACHE / "refs"
CTX = ssl._create_unverified_context()


def req(path, method="GET", body=None, tok=None, raw=False):
    h = {"Content-Type": "application/json"}
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    r = urllib.request.Request(f"{B}{path}", method=method, headers=h,
                               data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(r, context=CTX, timeout=120) as resp:
        data = resp.read()
    return data if raw else json.loads(data)


_NOT_FULL = re.compile(r"(?i)\b(sign|song|forced|karaoke|op/ed|commentary)")


def pick_ref_track(meta):
    """The embedded English 'full dialogue' text track, or None. Skips
    signs/songs/forced tracks — they'd grade a real full sub as wrong."""
    best = None
    for s in meta.get("subtitles") or []:
        lang = (s.get("language") or "").lower()
        if lang not in ("eng", "en"):
            continue
        if _NOT_FULL.search(f"{s.get('title') or ''} {s.get('label') or ''}"):
            continue
        if best is None:
            best = s
    return best


def imdb_ids(pairs):
    """{(kind, tmdb_id): imdb_id} via Wikidata SPARQL. P4983 = TMDb TV series,
    P4947 = TMDb movie."""
    out = {}
    for kind, prop in (("tv", "P4983"), ("movie", "P4947")):
        ids = sorted({str(t) for k, t in pairs if k == kind})
        for i in range(0, len(ids), 50):
            vals = " ".join(f'"{t}"' for t in ids[i:i + 50])
            q = f"SELECT ?t ?imdb WHERE {{ VALUES ?t {{ {vals} }} ?x wdt:{prop} ?t; wdt:P345 ?imdb. }}"
            url = "https://query.wikidata.org/sparql?" + urllib.parse.urlencode({"query": q, "format": "json"})
            r = urllib.request.Request(url, headers={"User-Agent": "StreamLink-subs-eval/1.0"})
            with urllib.request.urlopen(r, timeout=60) as resp:
                rows = json.load(resp)["results"]["bindings"]
            for row in rows:
                imdb = row["imdb"]["value"]
                if imdb.startswith("tt"):
                    out.setdefault((kind, int(row["t"]["value"])), imdb)
    return out


def spread(xs, n):
    if len(xs) <= n:
        return xs
    return [xs[round(i * (len(xs) - 1) / (n - 1))] for i in range(n)]


def main():
    REFS.mkdir(parents=True, exist_ok=True)
    tok = req("/api/admin/login", "POST", {"password": ADMIN_PW})["token"]
    items = req("/api/admin/library", tok=tok)["items"]
    cache = req("/api/admin/offline-cache", tok=tok)["items"]
    key_of = {f["file_path"]: f["cache_key"] for it in cache for f in it.get("files") or []
              if f.get("status") == "cached"}
    print(f"{len(items)} items, {len(key_of)} cached bundles")

    by_show = {}
    for it in items:
        try:
            md = req(f"/api/library/{it['id']}/metadata", tok=tok)
        except Exception as e:
            print("  metadata failed", it["title"][:60], e)
            continue
        m = md.get("metadata") or {}
        if not m.get("tmdb_id"):
            continue
        files = req(f"/api/library/{it['id']}/files", tok=tok)["files"]
        for f in files:
            if f["path"] not in key_of:
                continue
            show = (m.get("tmdb_kind"), m["tmdb_id"])
            by_show.setdefault(show, {"m": m, "anime": bool(md.get("anime")), "files": []})
            by_show[show]["files"].append((it, f))

    targets = []
    for show, g in by_show.items():
        m = g["m"]
        fs = sorted(g["files"], key=lambda x: (x[1].get("season") or 0, x[1].get("episode") or 0, x[1]["name"]))
        for it, f in spread(fs, PER_SHOW * 3):          # spare picks: not every bundle has a ref
            if sum(1 for t in targets if t["tmdb_id"] == m["tmdb_id"]) >= PER_SHOW:
                break
            key = key_of[f["path"]]
            try:
                meta = req(f"/api/library/offline-cache/{key}/meta.json")
            except Exception:
                continue
            tr = pick_ref_track(meta)
            if not tr:
                continue
            dest = REFS / f"{key}.vtt"
            if not dest.exists():
                dest.write_bytes(req(f"/api/library/offline-cache/{key}/{tr['file']}", raw=True))
            year = (m.get("first_air_date") or m.get("release_date") or "")[:4]
            targets.append({
                "tmdb_id": m["tmdb_id"], "kind": m.get("tmdb_kind"), "title": m.get("title"),
                "aka": m.get("aka") or [], "year": int(year) if year.isdigit() else None,
                "anime": g["anime"], "item_title": it["title"],
                "file": f["name"], "size": f.get("size_bytes"),
                "season": f.get("season"), "episode": f.get("episode"), "abs_no": f.get("abs_no"),
                "duration": meta.get("duration_sec"),
                "cache_key": key, "ref_track": tr.get("label"),
            })
            time.sleep(0.05)
        print(f"  {m.get('title')}: {sum(1 for t in targets if t['tmdb_id'] == m['tmdb_id'])}")

    ids = imdb_ids({(t["kind"], t["tmdb_id"]) for t in targets})
    for t in targets:
        t["imdb"] = ids.get((t["kind"], t["tmdb_id"]))
    miss = sorted({t["title"] for t in targets if not t["imdb"]})
    (HERE / "corpus.json").write_text(json.dumps(targets, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"{len(targets)} targets over {len({t['tmdb_id'] for t in targets})} shows; no IMDb id: {miss}")


if __name__ == "__main__":
    main()
