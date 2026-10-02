"""Spoken answers about the library, for Siri (20.4.0).

Two jobs, both pure:

  * **Which title did they mean?** Siri hands over what it heard ("star wars",
    "the hunter hunter"), never a library id. `rank` scores that against the
    names the library prints. A year, an article or a dropped "x" must not cost
    a match; a shared word alone must not make one.
  * **What is the answer, as a sentence?** `describe` (one title) and `overview`
    (everything on the move) turn download + prep facts into what Siri reads
    out. The sentence is built HERE, on the server, so the wording changes with
    a host update and the phone never needs a new build for it.

A sentence only claims what its facts carry: no percentage until the torrent
has a size, no time left until there is an estimate, and "finished" only when
nothing is still downloading.

Leaf module: stdlib only, no `main` import. Tests in tests/test_voicestatus.py.
Wired in main.py (`_voice_groups`, `_voice_facts`, `/api/voice/titles`,
`/api/voice/status`) and in the app (ios-app/ios/App/App/SiriIntents.swift).
"""

from __future__ import annotations

import difflib
import re

# Below this a name is not offered as a match at all.
MATCH_MIN = 0.5

_STOP = frozenset(("the", "a", "an", "of", "and"))
_YEAR = re.compile(r"^(19|20)\d\d$")


def tokens(s: str) -> list[str]:
    """Lower-case words of a name, punctuation and articles dropped."""
    words = re.findall(r"[a-z0-9]+", (s or "").lower().replace("'", "").replace("’", ""))
    kept = [w for w in words if w not in _STOP]
    return kept or words


def _sans_year(toks: list[str]) -> list[str]:
    kept = [t for t in toks if not _YEAR.match(t)]
    return kept or toks


def score(query: str, name: str) -> float:
    """How well a spoken `query` names `name`, 0..1."""
    q = tokens(query)
    n = tokens(name)
    if not q or not n:
        return 0.0
    # "Star Wars (1977)" is called "star wars"; the year only counts when said.
    forms = [n] if any(_YEAR.match(t) for t in q) else [n, _sans_year(n)]
    best = 0.0
    for f in forms:
        if q == f:
            return 1.0
        fs = set(f)
        hit = sum(1 for t in q if t in fs)
        if hit == len(q):
            # Everything said is in the name: the shorter the name, the likelier
            # it is the one meant ("star wars" → the film, not "... Rebels").
            best = max(best, 0.7 + 0.2 * len(q) / len(f))
            continue
        # Dictation slips ("hunter hunter", "andoor") still read close as text.
        ratio = difflib.SequenceMatcher(None, " ".join(q), " ".join(f)).ratio()
        if ratio >= 0.8:
            best = max(best, 0.8 * ratio)
        elif len(q) > 1 and hit / len(q) >= 0.6:
            best = max(best, 0.3 + 0.3 * hit / len(q))
    return best


def rank(query: str, names: list[str]) -> list[tuple[int, float]]:
    """`(index, score)` of every name that matches, best first; ties keep the
    order given, so the caller's ordering (downloading first) breaks them."""
    scored = [(i, score(query, n)) for i, n in enumerate(names)]
    return sorted([x for x in scored if x[1] >= MATCH_MIN], key=lambda x: -x[1])


def span(secs: float) -> str:
    """A duration as it is said aloud."""
    s = max(0, int(secs))
    if s < 60:
        return "less than a minute"
    m = int(round(s / 60.0))
    if m < 60:
        return "1 minute" if m == 1 else "%d minutes" % m
    if m >= 48 * 60:
        return "%d days" % int(round(m / 1440.0))
    m = int(round(m / 5.0)) * 5
    h, rem = divmod(m, 60)
    hours = "1 hour" if h == 1 else "%d hours" % h
    return hours if not rem else "%s %d minutes" % (hours, rem)


def _pct(f: dict) -> "int | None":
    total = int(f.get("total_bytes") or 0)
    if total <= 0:
        return None
    # Never rounds UP to 100: "100% downloaded" beside "not finished" is a lie.
    return min(99, int(100.0 * int(f.get("done_bytes") or 0) / total))


def _prep_tail(f: dict) -> str:
    files = int(f.get("files") or 0)
    if not f.get("prep") or files <= 0:
        return ""
    ready = int(f.get("prep_ready") or 0)
    busy = int(f.get("prep_busy") or 0)
    eta = f.get("prep_eta_secs")
    if ready >= files:
        return " It's prepped and ready to stream."
    left = (", about %s to go" % span(eta)) if (busy and eta is not None) else ""
    if files == 1:
        return (" It's being prepped now%s." % left) if busy else " It hasn't been prepped yet."
    if busy:
        return " %d of %d episodes are prepped%s." % (ready, files, left)
    return " %d of %d episodes are prepped." % (ready, files)


def describe(f: dict) -> str:
    """The answer for one title. `f` carries:

    name, items, downloading (item counts), done_bytes / total_bytes and
    eta_secs (-1 unknown) over the items still downloading, paused, stalled,
    finding_peers, errors, error; and for prep: prep (supported at all), files,
    prep_ready, prep_busy, prep_eta_secs (None unknown).
    """
    name = f.get("name") or "That"
    items = int(f.get("items") or 0)
    downloading = int(f.get("downloading") or 0)
    errors = int(f.get("errors") or 0)

    if downloading:
        pct = _pct(f)
        if f.get("finding_peers"):
            return "Not yet. %s hasn't started downloading; it's still finding peers." % name
        if pct is None:
            # Queued, or the server has not measured it since it started.
            return "Not yet. %s is still downloading." % name
        if items > 1:
            lead = "Not yet. %d of %d downloads for %s are still going, %d%% done" % (
                downloading, items, name, pct)
        else:
            lead = "Not yet. %s is %d%% downloaded" % (name, pct)
        eta = f.get("eta_secs")
        if f.get("paused"):
            return lead + ", and it's waiting for the idle download window."
        if f.get("stalled"):
            return lead + ", but it hasn't made progress in a while."
        if eta is not None and eta >= 0:
            return lead + ", about %s left." % span(eta)
        return lead + "."

    if errors and errors >= items:
        why = (f.get("error") or "").strip().rstrip(".")
        return "%s failed to download%s." % (name, (": " + why) if why else "")

    out = "Yes, %s has finished downloading." % name
    if errors:
        out += " %d download%s failed." % (errors, "" if errors == 1 else "s")
    return out + _prep_tail(f)


def overview(rows: list[dict]) -> str:
    """Everything still downloading, in one breath. `rows` are `describe` facts."""
    live = [r for r in rows if int(r.get("downloading") or 0)]
    if not live:
        return "Nothing is downloading right now."
    parts = []
    for r in live[:4]:
        pct = _pct(r)
        if r.get("finding_peers"):
            parts.append("%s, still finding peers" % r.get("name"))
        elif pct is None:
            parts.append("%s" % r.get("name"))
        elif r.get("eta_secs") is not None and r["eta_secs"] >= 0 and not r.get("paused"):
            parts.append("%s at %d%%, about %s left" % (r.get("name"), pct, span(r["eta_secs"])))
        else:
            parts.append("%s at %d%%" % (r.get("name"), pct))
    more = len(live) - len(parts)
    if more:
        parts.append("and %d more" % more)
    if len(live) == 1:
        return "One thing is downloading: %s." % parts[0]
    return "%d things are downloading: %s." % (len(live), "; ".join(parts))
