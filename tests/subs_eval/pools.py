"""Run every query shape for every target and summarise what comes back —
searches only, nothing downloaded. Feeds the design of the ranking."""
import json, re, sys
from pathlib import Path
from urllib.parse import quote
import common

API = "https://rest.opensubtitles.org/search"


def norm_title(t):
    t = re.sub(r"[【】\[\]()]", " ", t)
    t = t.lower().replace("'", "").replace("’", "")
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def q_url(**p):
    """Legacy REST wants its path params in alphabetical order, the query
    lower-case and '+'-joined — anything else 302s to a dead host."""
    segs = []
    for k in sorted(p):
        v = p[k]
        if v in (None, ""):
            continue
        if k == "query":
            v = quote(norm_title(str(v)), safe="").replace("%20", "+")
        segs.append(f"{k}-{v}")
    return f"{API}/" + "/".join(segs)


def queries(t):
    out = {}
    imdb = (t.get("imdb") or "").removeprefix("tt")
    tv = t["kind"] == "tv"
    if imdb:
        out["imdb"] = q_url(imdbid=imdb.zfill(7), season=t["season"] if tv else None,
                            episode=t["episode"] if tv else None, sublanguageid="eng")
    out["title"] = q_url(query=t["title"], season=t["season"] if tv else None,
                         episode=t["episode"] if tv else None, sublanguageid="eng")
    if tv and t.get("anime") and t.get("abs_no"):
        out["title_abs"] = q_url(query=t["title"], episode=t["abs_no"], sublanguageid="eng")
    out["old"] = f"{API}/query-{quote(Path(t['file']).stem)}/sublanguageid-eng"
    return out


def main():
    c = json.load(open(common.HERE / "corpus.json", encoding="utf-8"))
    for t in c:
        row = []
        for name, url in queries(t).items():
            res = common.os_get(url)
            n = len(res) if res is not None else "ERR"
            ok = 0
            if res:
                imdb = int((t.get("imdb") or "tt0").removeprefix("tt"))
                for r in res:
                    same_show = imdb and imdb in (int(r.get("SeriesIMDBParent") or 0), int(r.get("IDMovieImdb") or 0))
                    same_ep = t["kind"] != "tv" or (str(r.get("SeriesSeason")) == str(t["season"])
                                                    and str(r.get("SeriesEpisode")) == str(t["episode"]))
                    ok += bool(same_show and same_ep)
            row.append(f"{name}={n}/{ok}")
        print(f"{t['title'][:22]:22} S{t['season']}E{t['episode']:<3} " + "  ".join(row))


if __name__ == "__main__":
    main()
