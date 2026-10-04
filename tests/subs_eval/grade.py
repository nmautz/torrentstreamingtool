"""Download and grade the candidate pool for every target -> .cache/graded.json.

Pool = union of the IMDb, title and (anime) absolute-number searches, filtered
to the target's IMDb show when it has one, top MAX_PER by downloads. Each
download is graded against the file's embedded English track (common.timing).
Stops early if downloads stop being subtitles (throttled / blocked) — the box
shares this network's public IP, so hammering OpenSubtitles here hurts it too.
"""
import json, os, sys, time
import common, pools

MAX_PER = int(os.environ.get("SUBS_EVAL_MAX_PER", 10))


def pool(t):
    q = pools.queries(t)
    seen = {}
    for n in ("imdb", "title", "title_abs"):
        if n in q:
            for r in common.os_get(q[n]) or []:
                r = dict(r, _via=n)
                seen.setdefault(r["IDSubtitleFile"], r)
    imdb = int((t.get("imdb") or "tt0")[2:])
    rows = [r for r in seen.values()
            if not imdb or imdb in (int(r["SeriesIMDBParent"] or 0), int(r["IDMovieImdb"] or 0))]
    rows.sort(key=lambda r: -int(r["SubDownloadsCnt"] or 0))
    return rows[:MAX_PER]


def main():
    out_f = common.CACHE / "graded.json"
    graded = json.loads(out_f.read_text(encoding="utf-8")) if out_f.exists() else {}
    corpus = json.load(open(common.HERE / "corpus.json", encoding="utf-8"))
    bad_run = 0
    for t in corpus:
        ref = common.load_ref(t["cache_key"])
        for r in pool(t):
            k = f"{t['cache_key']}:{r['IDSubtitleFile']}"
            if k in graded:
                continue
            try:
                b = common.download(r["SubDownloadLink"])
            except common.BudgetExhausted as e:
                print("download budget reached:", e)
                bad_run = 99
                break
            if b is None and os.environ.get("SUBS_EVAL_OFFLINE"):
                continue
            cues = common.parse_subs(b, fps=float(r.get("MovieFPS") or 0) or 23.976)
            if not cues and bad_run < 99:
                bad_run += 1
                head = (b or b"")[:120]
                print("  NOT A SUB:", r["SubFileName"][:50], head)
                if bad_run >= 3:
                    print("three non-subtitle downloads in a row — stopping (throttled?)")
                    break
            else:
                bad_run = 0
            g = common.timing(ref, cues, t["duration"])
            g["content"] = round(common.content_score(ref, cues), 3)
            g["fmt"] = common.sub_format(b) if b else "?"
            g["ncues"] = len(cues)
            graded[k] = g
        else:
            out_f.write_text(json.dumps(graded), encoding="utf-8")
            print(f"{t['title'][:22]:22} S{t['season']}E{t['episode']}: {len(graded)} graded")
            continue
        break
    out_f.write_text(json.dumps(graded), encoding="utf-8")


if __name__ == "__main__":
    main()
