"""End-to-end simulation of the automatic fetch, per target, offline:

  filter (right show + episode) -> rank (rank.score_v1) -> for the top MAX_TRIES:
  align to the speech; accept the first whose confidence >= ACCEPT (correcting
  it when the fit says it's off), else skip the target entirely.

Outcome per target: what the viewer would get — sync / shift / drift / wrong
(a bad load) or none (skipped). Grades the loaded cues (after correction)
against the embedded reference.
"""
import collections, json, re, sys
import common, grade, features, proto_sync, align_eval, rank


def norm(s):
    s = (s or "").lower().replace("'", "").replace("’", "")
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def show_name(r):
    m = re.match(r'\s*"([^"]+)"', r.get("MovieName") or "")
    return m.group(1) if m else (r.get("MovieName") or "")


def filter_ok(t, r):
    imdb = int((t.get("imdb") or "tt0")[2:])
    if str(r.get("SubForeignPartsOnly")) == "1" or int(r.get("SubSumCD") or 1) > 1:
        return False
    if t["kind"] == "movie":
        if imdb:
            return int(r.get("IDMovieImdb") or 0) == imdb
        return norm(r.get("MovieName")) in {norm(x) for x in [t["title"], *t.get("aka", [])]}
    if imdb:
        if int(r.get("SeriesIMDBParent") or 0) != imdb:
            return False
    elif norm(show_name(r)) not in {norm(x) for x in [t["title"], *t.get("aka", [])]}:
        return False
    s, e = int(r.get("SeriesSeason") or 0), int(r.get("SeriesEpisode") or 0)
    if s == t["season"] and e == t["episode"]:
        return True
    return bool(t.get("anime") and t.get("abs_no") and e == t["abs_no"] and s in (0, 1))


def main(accept=5.0, max_tries=3, move=1.0, verbose=False):
    corpus = json.load(open(common.HERE / "corpus.json", encoding="utf-8"))
    g = json.load(open(common.CACHE / "graded.json"))
    al = json.load(open(common.CACHE / "aligned.json"))
    out = collections.Counter(); tries_used = collections.Counter()
    for t in corpus:
        ef = common.CACHE / "energy" / f"{t['cache_key']}.json"
        if not ef.exists():
            continue
        ref = common.load_ref(t["cache_key"])
        if len(ref) < 100:
            continue
        e = json.loads(ef.read_text())
        cands = [r for r in grade.pool(t) if filter_ok(t, r) and f"{t['cache_key']}:{r['IDSubtitleFile']}" in g]
        cands.sort(key=lambda r: -rank.score_v1(t, r))
        result = "none"
        for i, r in enumerate(cands[:max_tries]):
            k = f"{t['cache_key']}:{r['IDSubtitleFile']}"
            v = al.get(k)
            if v is None:
                cues = align_eval.cand_cues(r)
                a = proto_sync.align(cues, e, detrend=20, prior=1.25) if cues else None
                if not a:
                    continue
                v = {"conf": a["conf"], "ratio": a["ratio"], "offset": a["offset"]}
            if v["conf"] < accept:
                continue
            cues = align_eval.cand_cues(r)
            if v["ratio"] != 1.0 or abs(v["offset"]) >= move:
                cues = proto_sync.apply(cues, v["ratio"], v["offset"])
            result, _ = align_eval.regrade(t, ref, cues)
            tries_used[i + 1] += 1
            if verbose and result != "sync":
                print(f"  {t['title'][:16]:16} E{t['episode']:<3} loaded {result:5} conf {v['conf']:5.2f} {r['SubFileName'][:50]}")
            break
        out[result] += 1
    return out, tries_used


if __name__ == "__main__":
    for acc in (4.0, 5.0, 6.0, 7.0, 8.0):
        for mt in (1, 2, 3):
            o, tu = main(acc, mt)
            n = sum(o.values())
            print(f"accept>={acc} tries<={mt}: sync {o['sync']:2}  shift {o['shift']:2}  drift {o['drift']:2}  wrong {o['wrong']:2}  skipped {o['none']:2}  (n={n})  downloads {dict(tu)}")
    print()
    main(6.0, 3, verbose=True)
