"""Add targets whose only English track is an IMAGE subtitle (PGS/VOBSUB).

Those carry no text, but they carry exact timing — and timing alone grades sync
(and, via start/end offset agreement, lets almost no wrong episode through: 4/91
on the text-graded pool). The box renders the track to a subpack (subpack.py);
its manifest is a gap-free display list, which becomes timing-only cues here.

    python tests/subs_eval/collect_pgs.py      # appends to corpus.json, refs as .cache/refs/<key>.json

Builds ONE pack at a time (each is ~1-2 min of box CPU) and is resumable.
"""
import json, time
import collect

MIN_CUES = 100
SHOWS = {"Hunter x Hunter": 5, "Steins;Gate": 5, "Chernobyl": 3,
         "Star Wars: Episode III - Revenge of the Sith": 1}


def pack_cues(key, idx=0):
    st = collect.req(f"/api/library/offline-cache/{key}/subpack/{idx}", "POST", {})
    t0 = time.time()
    while st.get("state") not in ("ready", "error"):
        if time.time() - t0 > 900:
            raise TimeoutError(key)
        time.sleep(5)
        st = collect.req(f"/api/library/offline-cache/{key}/subpack/{idx}/status")
    if st["state"] != "ready":
        raise RuntimeError(f"{key}: {st}")
    man = collect.req(f"/api/library/offline-cache/{key}/subpack/{idx}/manifest.json")
    cues, seq = [], man.get("cues") or []
    for a, b in zip(seq, seq[1:] + [None]):
        if a.get("clear") or b is None:
            continue
        cues.append([float(a["t"]), float(b["t"]), ""])
    return cues, man


def main():
    corpus_f = collect.HERE / "corpus.json"
    corpus = json.loads(corpus_f.read_text(encoding="utf-8"))
    have = {t["cache_key"] for t in corpus}
    tok = collect.req("/api/admin/login", "POST", {"password": collect.ADMIN_PW})["token"]
    cache = collect.req("/api/admin/offline-cache", tok=tok)["items"]
    key_of = {f["file_path"]: f["cache_key"] for it in cache for f in it.get("files") or []
              if f.get("status") == "cached"}
    added = []
    for it in collect.req("/api/admin/library", tok=tok)["items"]:
        md = collect.req(f"/api/library/{it['id']}/metadata", tok=tok)
        m = md.get("metadata") or {}
        if m.get("title") not in SHOWS or not m.get("tmdb_id"):
            continue
        files = [f for f in collect.req(f"/api/library/{it['id']}/files", tok=tok)["files"]
                 if f["path"] in key_of and not f.get("bucket")
                 and (m.get("tmdb_kind") != "tv" or (f.get("season") and f.get("episode")))]   # no extras / featurettes
        files.sort(key=lambda f: (f.get("season") or 0, f.get("episode") or 0, f["name"]))
        for f in collect.spread(files, SHOWS[m["title"]]):
            key = key_of[f["path"]]
            prev = next((t for t in corpus if t["cache_key"] == key), None)
            if prev and len(json.loads((collect.REFS / f"{key}.json").read_text())) >= MIN_CUES:
                continue
            meta = collect.req(f"/api/library/offline-cache/{key}/meta.json")
            img = meta.get("skipped_image_subs") or []
            eng = [i for i, s in enumerate(img) if (s.get("language") or "") in ("eng", "en")]
            if meta.get("subtitles") or not eng:
                continue
            cues = []
            for idx in eng:                  # first English track is often signs/forced only
                try:
                    cues, man = pack_cues(key, idx)
                except Exception as e:
                    print("  pack failed", f["name"][:60], idx, e)
                    continue
                if len(cues) >= MIN_CUES:
                    break
            if len(cues) < MIN_CUES:
                print("  no full-dialogue track", f["name"][:60])
                if prev:
                    corpus.remove(prev)
                    corpus_f.write_text(json.dumps(corpus, indent=1, ensure_ascii=False), encoding="utf-8")
                continue
            if prev:
                corpus.remove(prev)
            (collect.REFS / f"{key}.json").write_text(json.dumps(cues))
            year = (m.get("first_air_date") or m.get("release_date") or "")[:4]
            t = {
                "tmdb_id": m["tmdb_id"], "kind": m.get("tmdb_kind"), "title": m.get("title"),
                "aka": m.get("aka") or [], "year": int(year) if year.isdigit() else None,
                "anime": bool(md.get("anime")), "item_title": it["title"],
                "file": f["name"], "size": f.get("size_bytes"),
                "season": f.get("season"), "episode": f.get("episode"), "abs_no": f.get("abs_no"),
                "duration": meta.get("duration_sec"), "cache_key": key,
                "ref_track": f"image:{img[idx].get('codec')}#{idx}", "ref_kind": "image",
            }
            corpus.append(t); added.append(t); have.add(key)
            print(f"  {m['title'][:24]} S{t['season']}E{t['episode']}: {len(cues)} cues")
            corpus_f.write_text(json.dumps(corpus, indent=1, ensure_ascii=False), encoding="utf-8")
    ids = collect.imdb_ids({(t["kind"], t["tmdb_id"]) for t in added})
    for t in corpus:
        if t in added:
            t["imdb"] = ids.get((t["kind"], t["tmdb_id"]))
    corpus_f.write_text(json.dumps(corpus, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"added {len(added)}; no IMDb: {sorted({t['title'] for t in added if not t.get('imdb')})}")


if __name__ == "__main__":
    main()
