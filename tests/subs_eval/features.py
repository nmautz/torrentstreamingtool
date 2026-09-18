"""Which search-result metadata predicts a right, in-sync subtitle? Offline —
reads the cached searches + graded.json, prints per-feature outcome tables."""
import collections, json, re
from pathlib import Path
import common, grade

RIGHT = 0.30          # aligned-text threshold (wrong-episode pairs top out ~0.25)


def label(t, x):
    if t.get("ref_kind") == "image":           # timing-only ref
        if x["off_a"] is None or x["off_b"] is None or abs(x["off_a"] - x["off_b"]) > 1.0 or x["iou"] < 0.5:
            return "wrong"
    elif x["text"] < RIGHT:
        return "wrong"
    return {"sync": "sync", "shift": "shift"}.get(x["sync"], "drift")


def last_ts(r):
    v = str(r.get("SubLastTS") or "")
    m = re.fullmatch(r"(\d+):(\d\d):(\d\d)", v)
    if m:
        return int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3])
    if v.isdigit():                                  # MicroDVD: frames
        fps = float(r.get("MovieFPS") or 0) or 23.976
        return int(v) / fps
    return None


_SRC = [("bluray", r"blu-?ray|bdrip|brrip|\bbd\b|bdremux|\bbd\d"), ("web", r"web-?dl|webrip|\bweb\b|amzn|nf\b|netflix|hmax|dsnp|hulu|atvp|\bcr\b"),
        ("hdtv", r"hdtv|pdtv|dsr|tvrip"), ("dvd", r"dvd")]


def source(s):
    s = (s or "").lower()
    for name, rx in _SRC:
        if re.search(rx, s):
            return name
    return ""


def group(s):
    s = s or ""
    m = re.match(r"\s*\[([^\]]+)\]", s)
    if m:
        return m[1].lower()
    m = re.search(r"-([A-Za-z0-9]+)(?:\[[^\]]*\])?(?:\.[a-z]{2,3})*$", Path(s).stem if "." in s[-5:] else s)
    return m[1].lower() if m else ""


def rows():
    corpus = json.load(open(common.HERE / "corpus.json", encoding="utf-8"))
    g = json.load(open(common.CACHE / "graded.json"))
    for t in corpus:
        for r in grade.pool(t):
            x = g.get(f"{t['cache_key']}:{r['IDSubtitleFile']}")
            if x:
                yield t, r, x, label(t, x)


def table(name, keyf):
    c = collections.defaultdict(collections.Counter)
    for t, r, x, lab in rows():
        c[keyf(t, r, x)][lab] += 1
    print(f"\n== {name}")
    for k in sorted(c, key=str):
        v = c[k]; n = sum(v.values())
        print(f"  {str(k):28} n={n:3}  sync {v['sync']/n:4.0%}  shift {v['shift']/n:4.0%}  drift {v['drift']/n:4.0%}  wrong {v['wrong']/n:4.0%}")


def main():
    def ratio(t, r, x):
        lt = last_ts(r)
        if not lt or not t.get("duration"):
            return "?"
        d = lt - t["duration"]
        return ("< -120" if d < -120 else "-120..-45" if d < -45 else "-45..-10" if d < -10 else
                "-10..+2" if d <= 2 else "+2..+30" if d <= 30 else "> +30")
    table("SubLastTS minus video duration (s)", ratio)
    table("source match (file vs sub release)", lambda t, r, x: (source(t["file"]) or "?") + " / " + (source(r.get("MovieReleaseName") or r.get("SubFileName")) or "?"))
    table("release group match", lambda t, r, x: "same" if group(t["file"]) and group(t["file"]) == group(r.get("MovieReleaseName") or "") or group(t["file"]) == group(r.get("SubFileName") or "") and group(t["file"]) else "diff")
    table("format", lambda t, r, x: r.get("SubFormat"))
    table("hearing impaired", lambda t, r, x: r.get("SubHearingImpaired"))
    table("from trusted", lambda t, r, x: r.get("SubFromTrusted"))
    table("user rank", lambda t, r, x: r.get("UserRank") or "-")
    table("auto-translation", lambda t, r, x: r.get("SubAutoTranslation"))
    table("fps", lambda t, r, x: r.get("MovieFPS"))
    table("downloads rank in pool", lambda t, r, x: "top" if r is grade.pool(t)[0] else "other")
    table("anime", lambda t, r, x: t.get("anime"))


if __name__ == "__main__":
    main()
