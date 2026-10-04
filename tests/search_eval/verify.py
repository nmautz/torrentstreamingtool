"""Re-run the 39 targets against the LIVE box (16.5.0) and score what the page
would now show, against the same hand labels. Mirrors _ssEpisodeQueries +
_ssEpisodeAccept in static/index.html."""
import json, re, ssl, sys, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor

B = "https://192.168.0.106"
CTX = ssl._create_unverified_context()
CC = {"US": "US", "UK": "GB", "AU": "AU", "NZ": "NZ", "CA": "CA"}

def get(path, **q):
    with urllib.request.urlopen(f"{B}{path}?{urllib.parse.urlencode(q)}", context=CTX, timeout=180) as r:
        return json.load(r)

def titles_for(m):
    norm = lambda t: (t or "").strip().lower()
    cand = [m["title"]] + list(m.get("aka") or [])
    allv = [norm(t) for t in cand if norm(t)]
    out = []
    for t in cand:
        k = norm(t)
        letters = [ch for ch in t if ch.isalpha()]
        if letters and sum(1 for ch in letters if ch.isascii()) / len(letters) < 0.6:
            continue
        if not k or any(norm(u) == k for u in out) or any(k.startswith(v + " ") or k.startswith(v + ":") for v in allv):
            continue
        out.append(t.strip())
        if len(out) == 3:
            break
    return out

def show_years(m, season):
    """16.6.0: the requested season's year or the show's first — not any season."""
    ys = []
    eps = ((m.get("seasons") or {}).get(str(season)) or {}).get("episodes") or [{}]
    for d in [m.get("first_air_date"), (eps[0] or {}).get("air_date")]:
        y = (d or "")[:4]
        if y.isdigit():
            ys.append(int(y))
    return ys

def ep_name(m, season, episode):
    eps = ((m.get("seasons") or {}).get(str(season)) or {}).get("episodes") or []
    e = next((x for x in eps if int(x.get("episode") or 0) == episode), None)
    return (e or {}).get("name") or ""

_DROP = {"the","a","an","of","to","and","in","on","at","for","with","from","episode","chapter","ep","pt"}
_TAG = re.compile(r"(?i)^(\d{3,4}p|2160p|4k|uhd|web|webdl|web-dl|webrip|hdtv|pdtv|bluray|blu-ray|bdrip|brrip|dvdrip|"
                  r"remux|x26[45]|h ?26[45]|hevc|avc|xvid|divx|av1|aac.*|ac3|dts.*|ddp?\d?|dd|flac.*|opus|atmos|repack|"
                  r"proper|internal|uncut|extended|ws|hr|dubbed|subbed|multi.*|vostfr|swesub|subfrench|french|truefrench|"
                  r"german|spanish|ita|eng|spa|jap|jpn|nl|nf|amzn|hulu|pcok|dsnp|atvp|hmax|max|cr|all4|ip|bbc|itv|iqy|viu|"
                  r"shahid|mp4|mkv|avi|ts|e\d{1,3}|v\d|s\d{1,2}|part|cour|batch|complete)$")

def _key(t):
    return {w for w in re.sub(r"[^a-z0-9 ]", " ", (t or "").lower().replace("'", "")).split()
            if w not in _DROP and not w.isdigit()}

def ep_verdict(m, res, season, episode):
    want = _key(ep_name(m, season, episode))
    mm = re.search(r"[Ss]\d{1,2}[Ee]\d{1,3}(?!\d)", res["title"] or "")
    if not want or not mm:
        return 0
    seg = []
    for w in re.split(r"[ ._\[\]()]+", res["title"][mm.end():]):
        if not w:
            continue
        if _TAG.match(w) or ("-" in w and len(w) > 6):
            break
        seg.append(w)
        if len(seg) >= 8:
            break
    got = _key(" ".join(seg))
    return 0 if not got else (1 if got & want else -1)

def article_ok(m, res):
    words = lambda t: re.sub(r"[^a-z0-9 ]", " ", (t or "").lower().replace("'", "")).split()
    name = words(res["title"])
    titles = [words(t) for t in [m["title"]] + list(m.get("aka") or []) if words(t)]
    if not titles:
        return True
    if any(all(i < len(name) and name[i] == w for i, w in enumerate(t)) for t in titles):
        return True
    if name and name[0] in ("the", "a", "an") and any(
            all(i + 1 < len(name) and name[i + 1] == w for i, w in enumerate(t)) for t in titles):
        return False
    return True

def tier(m, res, season, episode):
    v = ep_verdict(m, res, season, episode)
    yr = [int(t) for t in re.split(r"[^0-9A-Za-z]+", res["title"] or "") if re.fullmatch(r"(19|20)\d{2}", t)]
    ys = show_years(m, season)
    yc = bool(yr) and any(abs(y - sy) <= 1 for y in yr for sy in ys)
    tags = [CC[t] for t in re.split(r"[^0-9A-Za-z]+", res["title"] or "") if t in CC]
    cc = bool(m.get("origin_country")) and m["origin_country"] in tags
    if v < 0 and not yc:
        return -1
    return 1 if (v > 0 or yc or cc) else 0

def accept(m, res, season):
    if (res.get("rel") or 0) < 0.95:
        return False
    toks = [t for t in re.split(r"[^0-9A-Za-z]+", res["title"] or "") if t]
    years = [int(t) for t in toks if re.fullmatch(r"(19|20)\d{2}", t)]
    ys = show_years(m, season)
    if years and ys and not any(abs(y - sy) <= 1 for y in years for sy in ys):
        return False
    tags = [CC[t] for t in toks if t in CC]
    cc = m.get("origin_country") or ""
    if tags and cc and cc not in tags:
        return False
    return article_ok(m, res)

def run(case):
    s, e = case["target"][2], case["target"][3]
    m = get("/api/tmdb/lookup", tmdb_id=case["tmdb_id"], kind="tv")["metadata"]
    titles = titles_for(m)
    code = f"S{s:02d}E{e:02d}"
    forms = [(t, f"{t} {code}") for t in titles] + [(t, f"{t} {e:02d}") for t in titles]
    label = {x["title"]: x["label"] for x in case["candidates"]}
    for t, q in forms:
        try:
            d = get("/api/search", q=q, aka=t)
        except Exception:
            continue
        shown = [x for g in d.get("groups", []) for x in g.get("results", [])
                 if x.get("kind") == "episode" and x.get("season") == s and x.get("episode") == e and accept(m, x, s)]
        if shown:
            shown.sort(key=lambda x: (-tier(m, x, s, e), -(x.get("seeders") or 0)))
            return {"case": case, "query": q, "shown": shown, "country": m.get("origin_country"),
                    "labels": [label.get(x["title"]) for x in shown]}
    return {"case": case, "query": None, "shown": [], "country": m.get("origin_country"), "labels": []}

cases = [c for c in json.load(open(sys.argv[1], encoding="utf-8")) if "error" not in c]
with ThreadPoolExecutor(2) as ex:
    out = list(ex.map(run, cases))

tp = fp = unk = 0; top_ok = top_bad = top_none = missed = 0
for r in out:
    c = r["case"]
    known = [l for l in r["labels"] if l is not None]
    tp += sum(1 for l in known if l); fp += sum(1 for l in known if not l)
    unk += sum(1 for l in r["labels"] if l is None)
    had_right = any(x["label"] is True for x in c["candidates"])
    if not r["shown"]:
        missed += 1 if had_right else 0
        top_none += 1
    elif r["labels"][0] is True: top_ok += 1
    elif r["labels"][0] is False: top_bad += 1
    flag = "" if (r["labels"] and r["labels"][0] is True) else ("  <== nothing shown" if not r["shown"] else "  <== WRONG TOP")
    print(f'{c["title"][:26]:26} {c["first_air_date"][:4]} {str(r["country"]):3} shown={len(r["shown"]):3} '
          f'right={sum(1 for l in known if l):3} wrong={sum(1 for l in known if not l):3} '
          f'top={r["shown"][0]["title"][:48] if r["shown"] else "-"}{flag}')
print(f"\nLIVE 16.5.0: shown right={tp} wrong={fp} (unlabelled/new={unk})  precision={tp / max(1, tp + fp):.1%}")
print(f"top pick: right={top_ok} WRONG={top_bad} nothing shown={top_none} (of which had a right one: {missed})")
