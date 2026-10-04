"""Build the test set: for each target (show, season, episode), every release the
indexers return that the parser files as that exact episode, from the same query
forms ssSearchEpisode uses, with the rel the current rules would judge it by.
Output: cases.json (one entry per target, candidates un-labelled)."""
import json, ssl, sys, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor

B = "https://192.168.0.106"
CTX = ssl._create_unverified_context()

def get(path, **q):
    with urllib.request.urlopen(f"{B}{path}?{urllib.parse.urlencode(q)}", context=CTX, timeout=120) as r:
        return json.load(r)

# (search text, first-air year, season, episode)
TARGETS_2 = [
    ("Avatar: The Last Airbender", 2005, 1, 1), ("Avatar: The Last Airbender", 2024, 1, 1),
    ("Cowboy Bebop", 1998, 1, 1), ("Cowboy Bebop", 2021, 1, 1),
    ("One Piece", 1999, 1, 1), ("One Piece", 2023, 1, 1),
    ("Death Note", 2006, 1, 1), ("Naruto", 2002, 1, 1), ("Naruto Shippuden", 2007, 1, 1),
    ("Ghost in the Shell: Stand Alone Complex", 2002, 1, 1), ("Sailor Moon", 1992, 1, 1),
    ("Digimon Adventure", 1999, 1, 1), ("Digimon Adventure", 2020, 1, 1),
    ("Ghosts", 2019, 1, 1), ("Ghosts", 2021, 1, 1),
    ("Love Island", 2015, 1, 1), ("MasterChef", 1990, 1, 1), ("MasterChef", 2010, 1, 1),
    ("Taskmaster", 2015, 1, 1), ("Vikings", 2013, 1, 1), ("You", 2018, 1, 1), ("Lost", 2004, 1, 1),
    ("Gladiators", 2024, 1, 1), ("The Great British Bake Off", 2010, 1, 1),
    # later seasons: season confusion rather than show confusion
    ("Breaking Bad", 2008, 5, 1), ("Doctor Who", 2005, 4, 1), ("Frieren: Beyond Journey's End", 2023, 1, 13),
    ("Bleach", 2004, 1, 1), ("Hajime no Ippo", 2000, 1, 1), ("Sherlock", 2010, 2, 1),
]

TARGETS = [
    ("Mushi-Shi", 2005, 1, 1), ("Great Teacher Onizuka", 1999, 1, 1), ("Moomin", 1990, 1, 1),
    ("Doctor Who", 2005, 1, 1), ("Doctor Who", 1963, 1, 1), ("Doctor Who", 2024, 1, 1),
    ("Battlestar Galactica", 2004, 1, 1), ("Hunter x Hunter", 2011, 1, 1), ("Hunter x Hunter", 1999, 1, 1),
    ("Fullmetal Alchemist: Brotherhood", 2009, 1, 1), ("Fullmetal Alchemist", 2003, 1, 1),
    ("The Office", 2005, 1, 1), ("The Office", 2001, 1, 1), ("Shameless", 2011, 1, 1), ("Shameless", 2004, 1, 1),
    ("House of Cards", 2013, 1, 1), ("Twin Peaks", 1990, 1, 1), ("Berserk", 1997, 1, 1), ("Berserk", 2016, 1, 1),
    ("Trigun", 1998, 1, 1), ("Hellsing", 2001, 1, 1), ("Spy x Family", 2022, 2, 1),
    ("Attack on Titan", 2013, 4, 1), ("Utopia", 2013, 1, 1), ("Skins", 2007, 1, 1), ("Heroes", 2006, 1, 1),
    ("Lupin", 2021, 1, 1), ("Dragon Ball", 1986, 1, 1), ("Breaking Bad", 2008, 1, 1),
    ("Space Brothers", 2012, 1, 1), ("Land of the Lustrous", 2017, 1, 1), ("Queer as Folk", 2000, 1, 1),
    ("Being Human", 2008, 1, 1), ("Kaguya-sama: Love Is War", 2019, 1, 1), ("Oshi no Ko", 2023, 1, 1),
    ("Frieren: Beyond Journey's End", 2023, 1, 1), ("Solo Leveling", 2024, 1, 1), ("Dark", 2017, 1, 1),
    ("Money Heist", 2017, 1, 1), ("The Bridge", 2011, 1, 1),
]

def meta_for(name, year):
    res = get("/api/tmdb/search", query=name, kind="tv").get("results", [])
    pick = next((r for r in res if str(r.get("year")) == str(year)), None)
    if not pick:
        return None
    return get("/api/tmdb/lookup", tmdb_id=pick["id"], kind="tv").get("metadata")

def titles_for(m):
    """Same selection as _ssEpisodeQueries."""
    norm = lambda t: (t or "").strip().lower()
    cand = [m["title"]] + list(m.get("aka") or [])
    ot = m.get("original_title") or ""
    if ot and any(c.isascii() and c.isalpha() for c in ot):
        cand.append(ot)
    allv = [norm(t) for t in cand if norm(t)]
    out = []
    for t in cand:
        k = norm(t)
        if not k or any(norm(u) == k for u in out) or any(k.startswith(v + " ") or k.startswith(v + ":") for v in allv):
            continue
        out.append(t.strip())
        if len(out) == 3:
            break
    return out

def collect(target):
    name, year, s, e = target
    m = meta_for(name, year)
    if not m:
        return {"target": target, "error": "no tmdb match"}
    titles = titles_for(m)
    code = f"S{s:02d}E{e:02d}"
    forms = [(t, False, f"{t} {code}") for t in titles] + [(t, True, f"{t} {e:02d}") for t in titles]
    cands = {}
    for i, (t, bare, q) in enumerate(forms):
        try:
            d = get("/api/search", q=q, aka=t)
        except Exception as ex:
            continue
        for g in d.get("groups", []):
            for x in g.get("results", []):
                if x.get("kind") != "episode" or x.get("season") != s or x.get("episode") != e:
                    continue
                c = cands.setdefault(x["title"], {"title": x["title"], "seeders": x.get("seeders"), "forms": []})
                c["forms"].append({"i": i, "orig": i == 0, "bare": bare, "rel": x.get("rel")})
    seasons = {}
    for k, sv in (m.get("seasons") or {}).items():
        eps = sv.get("episodes") or []
        seasons[k] = {"name": sv.get("name"), "episodes": len(eps),
                      "year": ((eps[0].get("air_date") or "")[:4] if eps else "")}
    ep = next((x for x in (m.get("seasons") or {}).get(str(s), {}).get("episodes", []) if x.get("episode") == e), {})
    return {"target": target, "tmdb_id": m["tmdb_id"], "title": m["title"], "original_title": m.get("original_title"),
            "origin_country": m.get("origin_country") or "",
            "first_air_date": m.get("first_air_date"), "aka": m.get("aka"), "titles_searched": titles,
            "seasons": seasons, "episode_name": ep.get("name"), "episode_air": ep.get("air_date"),
            "candidates": sorted(cands.values(), key=lambda c: -(c["seeders"] or 0))[:CAP]}

CAP = int(sys.argv[3]) if len(sys.argv) > 3 else 40
sel = {"1": TARGETS, "2": TARGETS_2}.get(sys.argv[2] if len(sys.argv) > 2 else "1", TARGETS + TARGETS_2)
with ThreadPoolExecutor(3) as ex:
    out = list(ex.map(collect, sel))
json.dump(out, open(sys.argv[1], "w", encoding="utf-8"), ensure_ascii=False, indent=1)
for c in out:
    print(c["target"], c.get("error") or f'{c["title"]} ({c["first_air_date"]}) cands={len(c["candidates"])} titles={c["titles_searched"]}')
