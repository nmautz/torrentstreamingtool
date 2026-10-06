"""Bookmarks: a per-profile "watch later" list, and when something on it comes out.

A bookmark is a TMDb title (movie or show) someone wants to remember without
downloading it now. Most are already out and just sit there. The interesting
ones are **waiting** for something:

  * a movie still in theaters, waiting for its **digital** release -- the date a
    real home-quality copy can exist (see `_movie_release_flags` in main.py);
  * a movie or show that has not premiered yet;
  * a show whose **next season** has a premiere date.

For those the UI shows a countdown, and when the wait ends the bookmark is
flagged `new` until the person looks at their list. That flag is the whole
notification.

The flag is raised by a TRANSITION, never by a state: `advance` remembers what a
bookmark is `awaiting`, and only an awaited release that has now happened raises
it. So bookmarking a film that is already out does not light the dot, and a date
TMDb pushes back just moves the countdown. Missing evidence (TMDb unreachable, an
empty payload) never reads as a release: the caller passes `None` and nothing
changes.

Pure: stdlib only, no `main` import. Tests in `tests/test_bookmarks.py`.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Optional

# More than anyone curates by hand; a bound so a buggy client can't grow
# library.json without limit.
MAX_BOOKMARKS = 500

OUT = "out"             # nothing to wait for
THEATERS = "theaters"   # in cinemas, waiting for the digital release
UPCOMING = "upcoming"   # not out at all yet (or a new season is coming)

KINDS = ("movie", "tv")

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _day(s) -> str:
    """An ISO date, or "" for anything that isn't one."""
    s = (s or "")[:10] if isinstance(s, str) else ""
    return s if _DATE_RE.match(s) else ""


def key(kind: str, tmdb_id) -> str:
    return f"{kind}:{int(tmdb_id)}"


def normalize(raw) -> Optional[dict]:
    """A client-supplied candidate (the /api/tmdb/search shape) reduced to what
    a bookmark stores, or None if it doesn't name a TMDb title."""
    if not isinstance(raw, dict):
        return None
    kind = raw.get("kind")
    try:
        tid = int(raw.get("id") or 0)
    except (TypeError, ValueError):
        return None
    title = str(raw.get("title") or "").strip()
    if kind not in KINDS or tid <= 0 or not title:
        return None
    poster = str(raw.get("poster_path") or "")
    return {
        "kind": kind,
        "id": tid,
        "title": title[:200],
        "year": str(raw.get("year") or "")[:4],
        "poster_path": poster[:200] if poster.startswith("/") else "",
        "date": _day(raw.get("date")),
    }


def _nth_sunday(year: int, month: int, n: int) -> int:
    first = date(year, month, 1).weekday()          # Monday = 0
    return 1 + (6 - first) % 7 + 7 * (n - 1)


def us_eastern_day(now_utc: datetime) -> str:
    """The calendar date in US Eastern time, as an ISO day.

    The clock a US home-release date is judged by: a digital release lands at
    midnight Eastern, and TMDb gives only the day. Daylight time is worked out
    here (second Sunday of March 07:00 UTC to first Sunday of November 06:00
    UTC, the rule since 2007) because Windows ships no tz database."""
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    now_utc = now_utc.astimezone(timezone.utc)
    y = now_utc.year
    start = datetime(y, 3, _nth_sunday(y, 3, 2), 7, tzinfo=timezone.utc)
    end = datetime(y, 11, _nth_sunday(y, 11, 1), 6, tzinfo=timezone.utc)
    hours = -4 if start <= now_utc < end else -5
    return (now_utc + timedelta(hours=hours)).date().isoformat()


def movie_status(flags: dict, release_date: str, today: str) -> dict:
    """Where a movie is in its release, from `_movie_release_flags` output plus
    TMDb's primary `release_date`. `date` is what the countdown counts to and
    `what` names it: "digital" (home release) or "release" (first release of any
    kind, when no home date is known)."""
    digital = _day(flags.get("digital_date"))
    release = _day(release_date)
    if digital and digital <= today:
        return {"state": OUT, "date": digital, "what": "digital"}
    if digital:
        # A home date is announced and none has passed. Whether it's showing in
        # cinemas meanwhile only changes the badge, not the wait.
        opened = flags.get("theatrical_only") or (release and release <= today)
        return {"state": THEATERS if opened else UPCOMING, "date": digital, "what": "digital"}
    if flags.get("theatrical_only"):
        return {"state": THEATERS, "date": "", "what": "digital"}
    if release and release > today:
        return {"state": UPCOMING, "date": release, "what": "release"}
    return {"state": OUT, "date": "", "what": ""}


def tv_status(details: dict, today: str) -> dict:
    """Where a show is, from TMDb's `/tv/{id}` details: not premiered yet
    ("premiere"), a later season announced ("season", with `season`), or out."""
    first = _day(details.get("first_air_date"))
    if first and first > today:
        return {"state": UPCOMING, "date": first, "what": "premiere"}
    if not first and details.get("status") in ("Planned", "In Production", "Pilot"):
        return {"state": UPCOMING, "date": "", "what": "premiere"}
    nxt = details.get("next_episode_to_air") or {}
    try:
        season = int(nxt.get("season_number") or 0)
        ep = int(nxt.get("episode_number") or 0)
    except (TypeError, ValueError):
        season = ep = 0
    air = _day(nxt.get("air_date"))
    if season >= 2 and ep == 1 and air and air > today:
        return {"state": UPCOMING, "date": air, "what": "season", "season": season}
    return {"state": OUT, "date": "", "what": ""}


def _is_new_release(awaiting: dict, status: dict) -> bool:
    if status.get("state") == OUT:
        return True
    # Waiting for season N and TMDb now announces season N+1: N came out in
    # between (a slow refresh can miss the gap where nothing was upcoming).
    return (awaiting.get("what") == "season" and status.get("what") == "season"
            and int(status.get("season") or 0) > int(awaiting.get("season") or 0))


def advance(entry: dict, status: Optional[dict], now_iso: str) -> bool:
    """Apply a fresh status to one bookmark, in place. Returns True if anything
    changed (so the caller knows whether to write). `status=None` means no
    evidence this time -- nothing changes."""
    if not isinstance(status, dict) or status.get("state") not in (OUT, THEATERS, UPCOMING):
        return False
    before = (entry.get("status"), entry.get("awaiting"), entry.get("new"))
    awaiting = entry.get("awaiting") if isinstance(entry.get("awaiting"), dict) else None
    if awaiting and _is_new_release(awaiting, status):
        entry["new"] = now_iso
        entry.pop("awaiting", None)
        awaiting = None
    if status["state"] != OUT:
        entry["awaiting"] = dict(status)
    entry["status"] = dict(status)
    return before != (entry.get("status"), entry.get("awaiting"), entry.get("new"))


def mark_seen(entries: list, keys=None) -> bool:
    """Clear the `new` flag on every bookmark (or those whose key is in
    `keys`). Returns True if any flag was cleared."""
    changed = False
    for e in entries or []:
        if not isinstance(e, dict) or not e.get("new"):
            continue
        if keys is not None and key(e.get("kind", ""), e.get("id", 0)) not in keys:
            continue
        e.pop("new", None)
        changed = True
    return changed


def new_count(entries: list) -> int:
    return sum(1 for e in entries or [] if isinstance(e, dict) and e.get("new"))


def days_until(day: str, today: str) -> Optional[int]:
    """Whole days from `today` to `day` (negative once past), or None."""
    d, t = _day(day), _day(today)
    if not d or not t:
        return None
    return (date.fromisoformat(d) - date.fromisoformat(t)).days


def ordered(entries: list) -> list:
    """Display order: newly released first, then the soonest countdowns, then
    everything else most-recently bookmarked first."""
    def rank(e):
        st = e.get("status") or {}
        waiting = st.get("state") in (THEATERS, UPCOMING)
        return (0 if e.get("new") else 1 if waiting and st.get("date") else 2 if waiting else 3,
                st.get("date") or "" if waiting else "",
                _neg(e.get("added_at") or ""))
    return sorted([e for e in entries or [] if isinstance(e, dict)], key=rank)


def _neg(s: str) -> str:
    # Sort a string descending inside an ascending key.
    return "".join(chr(0x10FFFF - ord(c)) for c in s)
