"""Candidate rule improvements, measured before any of them ship."""
import re
from rule2 import ep_title_verdict, _words
CC = {"US": "US", "UK": "GB", "AU": "AU", "NZ": "NZ", "CA": "CA"}

def toks(t):
    return [x for x in re.split(r"[^0-9A-Za-z]+", t or "") if x]

def years_in(t):
    return [int(x) for x in toks(t) if re.fullmatch(r"(19|20)\d{2}", x)]

def season_year(c):
    s = str(c["target"][2])
    y = ((c.get("seasons") or {}).get(s) or {}).get("year") or ""
    return int(y) if str(y).isdigit() else None

def first_year(c):
    y = str(c.get("first_air_date") or "")[:4]
    return int(y) if y.isdigit() else None

def year_ok_strict(c, x):
    """A year in the name must be the requested season's year or the show's
    first-air year (+/-1). Any other season's year is not evidence for THIS
    episode: "One Piece S01E01 2023" is the live-action, not 1999 season 1."""
    ys = [y for y in (season_year(c), first_year(c)) if y]
    yr = years_in(x["title"])
    return not yr or not ys or any(abs(y - sy) <= 1 for y in yr for sy in ys)

def year_confirms(c, x):
    ys = [y for y in (season_year(c), first_year(c)) if y]
    yr = years_in(x["title"])
    return bool(yr) and any(abs(y - sy) <= 1 for y in yr for sy in ys)

def country_confirms(c, x):
    cc = c.get("origin_country") or ""
    return bool(cc) and cc in [CC[t] for t in toks(x["title"]) if t in CC]

def article_ok(c, x, parsed_name=None):
    """"The Dark" is not "Dark". Only the leading article differing is enough to
    reject, because it is the whole difference between two show names."""
    name = _words(parsed_name if parsed_name is not None else x["title"])
    for t in [c["title"]] + list(c.get("aka") or []):
        tw = _words(t)
        if not tw:
            continue
        if name[:len(tw)] == tw:
            return True
    # name starts with an article the show hasn't got (or vice versa)?
    for t in [c["title"]] + list(c.get("aka") or []):
        tw = _words(t)
        if tw and name and name[0] in ("the", "a", "an") and name[1:len(tw) + 1] == tw:
            return False
    return True

def evidence(c, x):
    """Ranking tier: a release whose year or episode title confirms the version
    outranks one with no evidence at all."""
    return 1 if (year_confirms(c, x) or ep_title_verdict(c, x) > 0 or country_confirms(c, x)) else 0
