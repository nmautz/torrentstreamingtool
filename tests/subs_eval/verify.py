"""Re-run the end-to-end simulation against the SHIPPING modules.

pipeline_eval.py measured prototypes; this drives `subsearch.py` and
`subsync.py` exactly as `main.py` does — filter, rank, download the top pick,
verify against the episode's speech, correct if the fit says so — and grades
the result against each file's embedded English track. Run it after touching a
weight in either module.

    SUBS_EVAL_OFFLINE=1 python tests/subs_eval/verify.py

Offline: everything it needs is in .cache/ (searches, downloads, speech energy)
from collect.py / grade.py / align_eval.py.
"""
import collections, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import common, grade, features, align_eval          # noqa: E402
import subsearch, subsync                           # noqa: E402

ACCEPT, MAX_TRIES = subsync.ACCEPT, 3


def target_of(t):
    """The corpus row as `main.py`'s `_subtitle_target` would build it."""
    return {"kind": t["kind"], "imdb": t.get("imdb"), "title": t["title"], "aka": t.get("aka") or [],
            "season": t["season"] if t["kind"] == "tv" else None,
            "episode": t["episode"] if t["kind"] == "tv" else None,
            "abs_no": t.get("abs_no"), "anime": t.get("anime"),
            "duration": t.get("duration"), "file_name": t["file"], "size": t.get("size"),
            "hash": None, "path": t["cache_key"]}


def main():
    corpus = json.load(open(common.HERE / "corpus.json", encoding="utf-8"))
    g = json.load(open(common.CACHE / "graded.json"))
    al = json.load(open(common.CACHE / "aligned.json"))
    out, tries = collections.Counter(), collections.Counter()
    old = collections.Counter()
    for t in corpus:
        ef = common.CACHE / "energy" / f"{t['cache_key']}.json"
        ref = common.load_ref(t["cache_key"])
        if not ef.exists() or len(ref) < 100:
            continue
        energy = json.loads(ef.read_text())
        pool = [r for r in grade.pool(t) if f"{t['cache_key']}:{r['IDSubtitleFile']}" in g]
        tgt = target_of(t)
        ranked = subsearch.rank(tgt, pool)
        # what the old code did: nothing reached the API at all
        old["none"] += 1
        got = "none"
        for i, r in enumerate(ranked[:MAX_TRIES]):
            cues = align_eval.cand_cues(r)
            if not cues:
                continue
            k = f"{t['cache_key']}:{r['IDSubtitleFile']}"
            v = al.get(k)
            fit = ({"ratio": v["ratio"], "offset": v["offset"], "conf": v["conf"],
                    "verified": v["conf"] >= ACCEPT,
                    "move": v["conf"] >= ACCEPT and (v["ratio"] != 1.0 or abs(v["offset"]) >= subsync.MOVE)}
                   if v else subsync.align(cues, energy))
            if not (fit and fit["verified"]):
                continue
            if fit["move"]:
                fn = subsync.mapper(fit)
                cues = [(fn(a), fn(b), txt) for a, b, txt in cues]
            got, _ = align_eval.regrade(t, ref, cues)
            tries[i + 1] += 1
            break
        out[got] += 1
        if got != "sync":
            print(f"  {t['title'][:16]:16} S{t['season']}E{t['episode']:<3} -> {got}")
    n = sum(out.values())
    print(f"\n{n} episodes | before (17.6.0): found nothing for all {n}")
    print(f"after: in sync {out['sync']}  <1s off {out['shift']}  drifting {out['drift']}  "
          f"wrong {out['wrong']}  skipped {out['none']}")
    print(f"downloads spent per episode: {dict(sorted(tries.items()))}")


if __name__ == "__main__":
    main()
