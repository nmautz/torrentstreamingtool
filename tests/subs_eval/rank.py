"""Score ranking strategies offline: for each target, which candidate would each
strategy put first, and how good is it? Also measures the auto-fetch gate
(strategy's confidence) as precision vs coverage.

    SUBS_EVAL_OFFLINE=1 python tests/subs_eval/rank.py
"""
import collections, json, math, re, sys
from pathlib import Path
import common, grade, features

CATS = ("sync", "shift", "drift", "wrong")

_STOPTOK = {"eng", "en", "english", "srt", "ass", "ssa", "sub", "hi", "sdh", "cc", "forced", "subs", "subtitles",
            "complete", "season", "episode", "the", "and", "of", "mkv", "mp4", "avi"}


def rel_tokens(name, title):
    s = (name or "").lower()
    s = re.sub(r"\.(srt|ass|ssa|sub|vtt|mkv|mp4|avi)$", "", s)
    toks = set(re.findall(r"[a-z0-9]+", s))
    toks -= set(re.findall(r"[a-z0-9]+", (title or "").lower()))
    toks = {t for t in toks if t not in _STOPTOK and not re.fullmatch(r"s\d+e\d+|s\d+|e\d+|\d{1,3}|\d+x\d+", t)}
    return toks


def rel_sim(t, r):
    a = rel_tokens(t["file"], t["title"])
    b = rel_tokens(r.get("MovieReleaseName"), t["title"]) | rel_tokens(r.get("SubFileName"), t["title"])
    if not a or not b:
        return 0.0
    return len(a & b) / len(a)


def score_v1(t, r):
    """Hand-built from features.py's tables."""
    s = 0.0
    lt = features.last_ts(r)
    d = t.get("duration")
    if lt and d:
        over = lt - d
        if over > 30:
            s -= 10                       # runs past the video: other cut / wrong episode
        elif over > 2:
            s -= 3
        elif over < -240:
            s -= 2                        # stops 4+ min early: partial or other cut
    fs, ss = features.source(t["file"]), features.source(r.get("MovieReleaseName") or r.get("SubFileName"))
    if fs and ss:
        s += 2 if fs == ss else -2
    s += 3 * rel_sim(t, r)
    fmt = (r.get("SubFormat") or "").lower()
    if fmt == "sub":
        s -= 4
    fps = float(r.get("MovieFPS") or 0)
    if fps and abs(fps - 25) < 0.01:
        s -= 2
    if str(r.get("SubAutoTranslation")) == "1":
        s -= 3
    if str(r.get("SubForeignPartsOnly")) == "1":
        s -= 10
    if str(r.get("SubBad")) not in ("0", ""):
        s -= 3
    if str(r.get("SubFromTrusted")) == "1":
        s += 0.5
    s += 0.25 * math.log10(1 + int(r.get("SubDownloadsCnt") or 0))
    return s


def score_downloads(t, r):
    return int(r.get("SubDownloadsCnt") or 0)


STRATS = {"downloads": score_downloads, "v1": score_v1}


def evaluate(strat, verbose=False):
    corpus = json.load(open(common.HERE / "corpus.json", encoding="utf-8"))
    g = json.load(open(common.CACHE / "graded.json"))
    top = collections.Counter(); per_show = collections.defaultdict(collections.Counter)
    for t in corpus:
        cands = []
        for r in grade.pool(t):
            x = g.get(f"{t['cache_key']}:{r['IDSubtitleFile']}")
            if x:
                cands.append((strat(t, r), r, features.label(t, x)))
        if not cands:
            continue
        cands.sort(key=lambda c: -c[0])
        top[cands[0][2]] += 1
        per_show[t["title"]][cands[0][2]] += 1
        if verbose and cands[0][2] != "sync":
            best = next((c for c in cands if c[2] == "sync"), None)
            print(f"  {t['title'][:16]:16} E{t['episode']:<3} picked {cands[0][2]:5} {cands[0][0]:6.2f} {cands[0][1]['SubFileName'][:45]:45}"
                  + (f" | sync was {best[0]:6.2f} {best[1]['SubFileName'][:45]}" if best else " | no sync available"))
    return top, per_show


def main():
    names = sys.argv[1:] or list(STRATS)
    for n in names:
        top, per = evaluate(STRATS[n], verbose="-v" in sys.argv or len(names) == 1)
        tot = sum(top.values())
        print(f"{n:10} n={tot}  " + "  ".join(f"{c} {top[c]:2}" for c in CATS))


if __name__ == "__main__":
    main()
