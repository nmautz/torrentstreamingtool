"""What counts as "watched" - the one completion rule for every player.

Split out of `main.py` so the rule can be tested without a server: plain dicts
in, a verdict out, no I/O, no `main` import. `tests/test_watchrule.py` drives it
with the exact sequences measured live against the box. `main.py` wraps it
(`_watch_state`) to look up the file's detected `credits_start`.

Completion used to be read off the PLAYHEAD ALONE: a file was credited once its
position sat within a few seconds of the outro. Driven live on a 23-minute
episode nobody had watched (17.5.0), that gave four false positives:

  * VLC, scrub to 99.6 % -> VLC ran out the last seconds, the playlist ended and
    the episode was written `completed` at pos == dur.
  * Device player, scrub to anywhere in the last ~10 s -> credited instantly,
    with no playback at all (HLS seeks also overshoot by ~5 s, widening it).
  * Device player, scrub into the final segment -> the element fired `ended`:
    credited AND auto-advanced into the next episode.
  * VLC, press Next 33 s in -> credited 60 s later by a deferred-watch timer that
    treated "moved on" as "finished", from any position.

A position is not evidence: the playhead reaches the end identically whether
the episode was watched or the scrub bar was dragged. So completion needs a
second, independent fact - how much of the file was ACTUALLY PLAYED.

`played_sec` is that fact, a per-profile watermark kept in each file_progress
record. Every progress write adds the position advance since the previous write,
CAPPED BY THE WALL CLOCK that elapsed between the two. Playback moves the
playhead at about real time and is credited in full; a seek moves it minutes in
a second and earns only that second. No client has to say which is which, so
the rule holds for the dashboard, the TV kiosk, the iOS app (web and native
background) and every stale build of them.

The one exception is the offline batch sync: the device's OfflineStore coalesces
an offline session into ONE final position, so there is no history to measure
and `offline_watch_state` falls back to the tail test alone.

See docs/LIBRARY_DATA.md and docs/GOTCHAS.md.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Optional

# A file with detected credits has reached its end within this many seconds of
# `credits_start`; one without detected credits, from FINISH_TAIL_PCT of runtime.
STOP_OUTRO_WINDOW_SEC = 10
# The old no-credits rule was "within 10 s of the real end", which almost nobody
# reaches: an anime episode's last ~95 s are the ED and the next-episode preview.
# Hunter x Hunter S01E02 stopped at 93.3 % stayed unwatched forever. Safe to widen
# now that the played-time test, not the position, keeps a scrub from counting.
FINISH_TAIL_PCT = 0.90
# ...and at least this much of the runtime must genuinely have been played. Leaves
# room to skip an intro, a recap and the credits; not room to scrub past the plot.
MIN_PLAYED_PCT = 0.60
# Content-seconds creditable per wall-clock second between two writes. Above 1x
# for 2x playback plus poll jitter; far below any seek.
PLAYED_RATE_CAP = 2.5
# No single write earns more than this, however long since the previous one.
# Every live writer saves every 15 s (VLC tracker, device player, iOS native), so
# this barely touches real playback. What it stops is a STALE record's age being
# spent on a scrub: stopped at 5:00 last week, open it, drag to the end - the
# week-long gap would otherwise pay for the whole episode.
PLAYED_STEP_CAP_SEC = 60


def _num(value, default: float = 0.0) -> float:
    """A finite, non-negative float, or `default`."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, v) if math.isfinite(v) else default


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    """Tolerant ISO-8601 -> aware UTC datetime; None if unparseable."""
    if not s:
        return None
    try:
        t = s.strip()
        if t.endswith("Z"):
            t = t[:-1] + "+00:00"
        dt = datetime.fromisoformat(t)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        return None


def tail_start(dur: float, credits_start: Optional[float]) -> float:
    """Position from which a file counts as having reached its end."""
    if dur <= 0:
        return math.inf
    if credits_start:
        return max(0.0, float(credits_start) - STOP_OUTRO_WINDOW_SEC)
    return dur * FINISH_TAIL_PCT


def played_after(prev: Optional[dict], pos: float, at_iso: str) -> float:
    """`played_sec` after a write landing at `pos` at time `at_iso`.

    A brand-new record starts at 0 - nothing is proven yet, which is what stops a
    first-ever write that is a scrub to the end from counting. A record from before
    `played_sec` existed is seeded from the position it already holds: the old rule
    believed that position was watched, and without the seed a half-finished
    episode could never accrue enough to finish."""
    if not prev:
        return 0.0
    base = prev.get("played_sec")
    base = _num(prev.get("position_sec") if base is None else base)
    step = _num(pos) - _num(prev.get("position_sec"))
    then, now = _parse_iso(prev.get("updated_at")), _parse_iso(at_iso)
    if step <= 0 or not (then and now):
        return round(base, 1)
    elapsed = (now - then).total_seconds()
    if elapsed <= 0:
        return round(base, 1)
    return round(base + min(step, elapsed * PLAYED_RATE_CAP, PLAYED_STEP_CAP_SEC), 1)


def watch_state(prev: Optional[dict], pos: float, dur: float,
                credits_start: Optional[float], at_iso: str, *,
                measure_play: bool = True) -> tuple[float, bool]:
    """(played_sec, completed) for a progress write. `completed` is monotonic.

    `measure_play=False` keeps the tail test and skips the played-time one - for a
    source whose single position carries no playback history."""
    prev = prev or {}
    played = played_after(prev or None, pos, at_iso)
    if prev.get("completed"):
        return played, True
    if dur <= 0 or pos < tail_start(dur, credits_start):
        return played, False
    return played, (not measure_play) or played >= dur * MIN_PLAYED_PCT


def offline_watch_state(prev: Optional[dict], pos: float, dur: float,
                        credits_start: Optional[float],
                        at_iso: str) -> tuple[float, bool]:
    """`watch_state` for the offline batch sync / conflict resolve.

    One coalesced position per file, no history: completion is the tail test
    alone, and the reported position is taken as played (never lowering what was
    already credited) - the same trust the pre-`played_sec` rule gave it - so an
    episode begun on a plane and finished on the sofa can still complete."""
    played, done = watch_state(prev, pos, dur, credits_start, at_iso,
                               measure_play=False)
    return max(played, round(_num(pos), 1)), done
