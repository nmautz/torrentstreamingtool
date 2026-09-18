"""Model x prompt benchmark over the merged labelled set.

    python bench.py labelled_all.json <port> <tag> [--prompt v2|v4] [--batch N]
                    [--reason] [--survivors] [--single]

--survivors  only ask the model about results that pass the shipped 16.5.0 rule
             (name >= 0.95 + year + country) — the two-stage design.
--single     one release per request instead of a batch.
Writes out_<tag>.json with a verdict per candidate.
"""
import json, re, sys, time, urllib.request

RULE_NAME = 0.95
CC = {"US": "US", "UK": "GB", "AU": "AU", "NZ": "NZ", "CA": "CA"}

# ---------- the shipped rule (mirrors _ssEpisodeAccept) ----------
def maxrel(x):
    return max((f["rel"] or 0) for f in x["forms"])

def show_years(c):
    ys = []
    for d in [c.get("first_air_date")] + [(s.get("year") or "") for s in (c.get("seasons") or {}).values()]:
        y = str(d or "")[:4]
        if y.isdigit():
            ys.append(int(y))
    return ys

def rule_ok(c, x):
    if maxrel(x) < RULE_NAME:
        return False
    toks = [t for t in re.split(r"[^0-9A-Za-z]+", x["title"] or "") if t]
    years = [int(t) for t in toks if re.fullmatch(r"(19|20)\d{2}", t)]
    ys = show_years(c)
    if years and ys and not any(abs(y - sy) <= 1 for y in years for sy in ys):
        return False
    tags = [CC[t] for t in toks if t in CC]
    cc = c.get("origin_country") or ""
    if tags and cc and cc not in tags:
        return False
    return True

# ---------- prompts ----------
BASE = """You check torrent release names against one TV show.
For each numbered release, first write the show name exactly as it appears in the release (the words before the episode number or SxxExx, without group tags in brackets, quality or language tags), then decide whether the release contains the requested episode of THIS show.

The show name in the release must be the show's title or one of its other names. Extra words make it a different show, even when the title is inside it: "Naruto Shippuden" is not "Naruto", "Star Trek Discovery" is not "Star Trek", "The Walking Dead World Beyond" is not "The Walking Dead", "Escaping Alcatraz" is not "Alcatraz".
A year in the release that differs from the show's first-air year by more than one means a different show (a remake or reboot). A country tag (US, UK, AU, NZ, CA) that is not the show's country means another country's version.
Season words (2nd Season, S2, Part 2, Final Season, a trailing 2) mean a later season; only accept them if that season is the one requested.
Movies, specials, OVAs, recaps, companion shows ("Extra", "Unleashed", "Confidential", "The Commentaries") and live-action adaptations are no.
Packs or batches that include the requested episode are yes.
If the name matches, and no year, country or season word contradicts it, answer yes."""

FEWSHOT = """

Worked examples (other shows):
- Show "Naruto" (2002, JP), want S1E1. Release "[Erai-raws] Boruto - Naruto Next Generations - 01" -> name "Boruto - Naruto Next Generations" -> no, that is the sequel series.
- Show "Star Trek: The Next Generation" (1987, US), want S1E1. Release "Star.Trek.TNG.S01E01.1080p.BluRay" -> name "Star Trek TNG" -> yes, TNG is an abbreviation of this show's own title.
- Show "The Bridge" (2011, SE), want S1E1. Release "The Bridge 2013 S01E01 Pilot 720p AMZN" -> name "The Bridge", year 2013 -> no, that is the 2013 American remake.
- Show "Ghosts" (2021, US), want S1E1. Release "Ghosts Australia S01E01 1080p" -> name "Ghosts Australia" -> no, the Australian version.
- Show "Death Note" (2006, JP), want S1E1. Release "[NOP] Death Note - 01 (2015 Drama Series)" -> name "Death Note", but "2015 Drama Series" -> no, the live-action drama.
- Show "Cowboy Bebop" (1998, JP), want S1E1. Release "[EG] Cowboy Bebop - 01 [BD 10-bit, Dual-Audio]" -> name "Cowboy Bebop" -> yes."""

PROMPTS = {"v2": BASE, "v4": BASE + FEWSHOT}

def show_block(c):
    s, e = c["target"][2], c["target"][3]
    seasons = "; ".join(f'season {k}: {v["episodes"]} episodes, {v["year"]}'
                        for k, v in (c.get("seasons") or {}).items() if k != "0")
    aka = ", ".join((c.get("aka") or [])[:6])
    return (f'Show: {c["title"]} (first aired {c["first_air_date"]}, country {c.get("origin_country") or "?"})\n'
            f'Other names: {aka or "none"}\n'
            f'Original title: {c.get("original_title") or c["title"]}\n'
            f'Seasons: {seasons}\n'
            f'Requested: season {s} episode {e}' + (f' ("{c["episode_name"]}")' if c.get("episode_name") else ""))

def ask(port, system, c, items, reason, retry=True):
    lines = "\n".join(f"{i + 1}. {x['title']}" for i, x in enumerate(items))
    user = f"{show_block(c)}\n\nReleases:\n{lines}\n\nAnswer for every release, in order."
    item = ({"type": "object", "properties": {"name": {"type": "string", "maxLength": 80},
                                              "match": {"type": "boolean"}}, "required": ["name", "match"]}
            if reason else {"type": "boolean"})
    schema = {"type": "object", "properties": {"answers": {"type": "array", "items": item,
              "minItems": len(items), "maxItems": len(items)}}, "required": ["answers"]}
    body = {"messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0, "max_tokens": (90 * len(items) + 150) if reason else (25 * len(items) + 100),
            "response_format": {"type": "json_schema", "json_schema": {"name": "a", "schema": schema}}}
    t = time.perf_counter()
    req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=1200) as r:
        d = json.load(r)
    wall = time.perf_counter() - t
    try:
        ans = json.loads(d["choices"][0]["message"]["content"])["answers"]
    except Exception:
        if len(items) > 1 and retry:
            h = len(items) // 2
            a1, t1 = ask(port, system, c, items[:h], reason); a2, t2 = ask(port, system, c, items[h:], reason)
            return a1 + a2, {k: (t1[k] or 0) + (t2[k] or 0) for k in t1}
        return [False] * len(items), {"wall": wall, "prompt_n": 0, "predicted_n": 0, "prompt_ms": 0, "predicted_ms": 0}
    ans = [a["match"] if isinstance(a, dict) else a for a in ans]
    tm = d.get("timings", {})
    return ans, {"wall": wall, "prompt_n": tm.get("prompt_n") or 0, "predicted_n": tm.get("predicted_n") or 0,
                 "prompt_ms": tm.get("prompt_ms") or 0, "predicted_ms": tm.get("predicted_ms") or 0}

if __name__ == "__main__":
    path, port, tag = sys.argv[1], sys.argv[2], sys.argv[3]
    reason = "--reason" in sys.argv
    survivors = "--survivors" in sys.argv
    system = PROMPTS[sys.argv[sys.argv.index("--prompt") + 1] if "--prompt" in sys.argv else "v2"]
    batch = 1 if "--single" in sys.argv else (int(sys.argv[sys.argv.index("--batch") + 1]) if "--batch" in sys.argv else 20)
    cases = [c for c in json.load(open(path, encoding="utf-8")) if "error" not in c]
    timings = []
    t0 = time.perf_counter()
    for n, c in enumerate(cases):
        items = [x for x in c["candidates"] if not survivors or rule_ok(c, x)]
        for x in c["candidates"]:
            x["llm"] = False
        for k in range(0, len(items), batch):
            chunk = items[k:k + batch]
            ans, tm = ask(port, system, c, chunk, reason)
            timings.append(tm)
            for x, a in zip(chunk, ans):
                x["llm"] = bool(a)
        if n % 10 == 0:
            print(f"  {n}/{len(cases)} {time.perf_counter() - t0:.0f}s", flush=True)
    json.dump({"cases": cases, "timings": timings, "tag": tag, "survivors": survivors},
              open(f"out_{tag}.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    pn = sum(t["prompt_n"] for t in timings); gn = sum(t["predicted_n"] for t in timings)
    pms = sum(t["prompt_ms"] for t in timings); gms = sum(t["predicted_ms"] for t in timings)
    print(f"[{tag}] {len(cases)} shows in {time.perf_counter() - t0:.0f}s; "
          f"prompt {pn} tok ({pn / max(1, pms) * 1000:.0f}/s), generated {gn} tok ({gn / max(1, gms) * 1000:.0f}/s), "
          f"{gn / len(cases):.0f} generated per show")
