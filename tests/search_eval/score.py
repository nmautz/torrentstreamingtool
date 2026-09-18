"""Score one or more bench runs: python score.py out_a.json out_b.json ..."""
import json, sys
from bench import rule_ok, maxrel

HARD_EP = False
from rule2 import ep_title_verdict
from rule3 import year_ok_strict, year_confirms, article_ok, evidence

def rule_v2(c, x):
    """shipped rule + strict season year + article check"""
    return rule_ok(c, x) and year_ok_strict(c, x) and article_ok(c, x)

def walk(c, ok, soft_ep=False):
    n = max((f["i"] for x in c["candidates"] for f in x["forms"]), default=-1) + 1
    for i in range(n):
        got = [x for x in c["candidates"] if ok(x) and any(f["i"] == i for f in x["forms"])]
        if got and soft_ep:
            # A year that matches the show already identifies the version, and
            # beats a title mismatch (TMDb's episode name and the scene's often
            # differ: "The Dragonslayer" vs "The Branded Swordsman").
            keep = [x for x in got if ep_title_verdict(c, x) >= 0 or year_confirms(c, x)]
            got = keep if (keep or globals()["HARD_EP"]) else got
        if got:
            key = (lambda x: (-evidence(c, x), -(x["seeders"] or 0))) if soft_ep else (lambda x: -(x["seeders"] or 0))
            return sorted(got, key=key)
    return []

def report(name, cases, ok, soft_ep=False):
    tp = fp = 0; t_ok = t_bad = t_none = missed = 0; bad_shows = []
    for c in cases:
        shown = walk(c, lambda x, c=c: ok(c, x), soft_ep)
        known = [x for x in shown if x["label"] is not None]
        tp += sum(1 for x in known if x["label"]); fp += sum(1 for x in known if not x["label"])
        had = any(x["label"] is True for x in c["candidates"])
        if not shown:
            t_none += 1; missed += 1 if had else 0
        elif shown[0]["label"] is True: t_ok += 1
        elif shown[0]["label"] is False:
            t_bad += 1; bad_shows.append(f'{c["title"][:18]} {c["first_air_date"][:4]}')
    print(f"  {name:26} right={tp:4} wrong={fp:4} prec={tp / max(1, tp + fp):5.1%} | "
          f"top: ok={t_ok:3} WRONG={t_bad:2} none={t_none:2} (missed {missed})")
    return bad_shows

runs = {}
for p in sys.argv[1:]:
    d = json.load(open(p, encoding="utf-8"))
    runs[d.get("tag") or p] = d["cases"]

first = list(runs.values())[0]
print(f"{len(first)} shows, {sum(len(c['candidates']) for c in first)} candidates")
bad = {}
bad["rule (shipped 16.5.0)"] = report("rule (shipped 16.5.0)", first, rule_ok)
bad["rule + ep-title (soft)"] = report("rule + ep-title (soft)", first, rule_ok, soft_ep=True)
bad["+ strict yr + article"] = report("+ strict yr + article", first, rule_v2, soft_ep=True)
globals()["HARD_EP"] = True
bad["+ hard ep-title"] = report("+ hard ep-title", first, rule_v2, soft_ep=True)
globals()["HARD_EP"] = False
for tag, cases in runs.items():
    bad[f"{tag} alone"] = report(f"{tag} alone", cases, lambda c, x: x.get("llm", False))
    bad[f"{tag} + rule"] = report(f"{tag} + rule", cases, lambda c, x: rule_ok(c, x) and x.get("llm", False))
    bad[f"{tag} + rule + ep"] = report(f"{tag} + rule + ep", cases, lambda c, x: rule_ok(c, x) and x.get("llm", False), soft_ep=True)
print("\nshows whose top pick is wrong:")
for k, v in bad.items():
    print(f"  {k:26} {', '.join(sorted(v)) or '-'}")
