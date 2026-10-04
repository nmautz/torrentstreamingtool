"""When has a download that HAS fetched something stopped for good?

The dead-swarm retry in `main.py` (`_note_download_stall` / `_retry_dead_download`)
only ever fires at **zero bytes**: a release that never sent anything is a dead
pick, and deleting it costs nothing. That left the other half of the problem
untouched - a torrent whose only seeder walks away at 70 % sits at "downloading"
for ever, with no error and no retry. Observed on the box: four SpongeBob
episodes at 41-83 %, 0 B/s, for twenty-four hours.

This module answers one question - "has this part-download been idle long enough
that it is not coming back on its own?" - and nothing else. What to DO about it
(race a replacement beside it, see `_rescue_stalled_download`) lives in `main.py`.

Three properties, each of which exists because the obvious version is wrong:

* **Idle time is ACCRUED, not measured from a timestamp.** The box restarts
  often (auto-update, scheduled reboot, the VPN watchdog) and qBittorrent is
  stopped whenever the VPN is down. A wall-clock "last byte at" would charge all
  of that against the torrent and condemn it the moment the service came back.
  Each tick adds at most `TICK_CAP` seconds, and only while the torrent was
  actually trying.
* **Patience grows with what would be thrown away.** Thirty idle minutes is
  plenty to give up on a 300 MB episode; it is not enough for a film with 16 GB
  already on disk.
* **A stuck torrent is never an error.** Its bytes are real and its seeder may
  return, so the verdict here only ever means "start looking for another copy".

Pure: plain dicts in, a verdict out, no clock (the caller passes `now`), no I/O,
no `main` import. Tests in `tests/test_stallrule.py`.
"""

from __future__ import annotations

from typing import Optional

GB = 1024 ** 3

# Idle seconds before a part-download with next to nothing on disk is judged
# stuck.
PATIENCE_BASE = 30 * 60
# ...plus this much for every GiB already fetched,
PATIENCE_PER_GB = 20 * 60
# ...up to here. A torrent that has not moved in six hours of trying is not
# having a slow afternoon.
PATIENCE_MAX = 6 * 3600
# The most one observation may add. The monitor ticks every 5 s; anything much
# longer than that between two looks is time we were not watching (a restart, a
# hung tick), and unobserved time must not count.
TICK_CAP = 30.0
# Idle this long and the UI stops saying "Downloading" - well before anything is
# done about it, because a card that reads "Downloading" at 0 B/s is the lie
# that hid this for a day.
SHOW_AFTER = 5 * 60
# How long to wait before looking for a replacement again, by attempt. The first
# retry is short because the commonest reason for a rescue not starting is the
# global race cap, which frees up within minutes.
RETRY_BACKOFF = (10 * 60, 30 * 60, 3600, 2 * 3600, 6 * 3600)

# qBittorrent states in which the torrent is genuinely trying to download. A
# paused, queued, checking or moving torrent is idle for a reason that is not
# the swarm's fault and must never be judged for it.
TRYING_STATES = frozenset({"downloading", "stalledDL", "forcedDL"})


def is_trying(qstate: str) -> bool:
    """True when qBittorrent is actively attempting to fetch this torrent."""
    return (qstate or "") in TRYING_STATES


def patience(completed: int) -> float:
    """Idle seconds this much fetched data has earned before it is given up on."""
    try:
        done = max(0, int(completed or 0))
    except (TypeError, ValueError):
        done = 0
    return float(min(PATIENCE_MAX, PATIENCE_BASE + PATIENCE_PER_GB * (done / GB)))


def advance(mark: Optional[dict], completed: int, now: float, trying: bool) -> dict:
    """Fold one observation into an item's stall mark and return the new mark.

    `mark` is `{"bytes", "idle", "seen", "tries", "retry_at"}`. Any growth in
    `completed` clears the idle clock AND the retry history - a torrent that is
    moving again is not the one we gave up on. Fewer bytes than last time (a
    recheck threw pieces away, or the item was repointed at another torrent)
    re-anchors without crediting or charging anything.
    """
    m = dict(mark) if isinstance(mark, dict) else {}
    try:
        done = max(0, int(completed or 0))
    except (TypeError, ValueError):
        done = 0
    prev = int(m.get("bytes") or 0)
    seen = float(m.get("seen") or 0.0)
    if done != prev or not seen:
        if done > prev or not seen:
            m["tries"] = 0
            m["retry_at"] = 0.0
        m["bytes"] = done
        m["idle"] = 0.0
    elif trying:
        gap = now - seen
        if gap > 0:
            m["idle"] = float(m.get("idle") or 0.0) + min(gap, TICK_CAP)
    m["seen"] = float(now)
    return m


def is_stuck(mark: Optional[dict]) -> bool:
    """True once a PART-download has been idle past its patience.

    Zero bytes is deliberately False: that case belongs to the dead-swarm retry,
    which is quicker (ten minutes) and may simply delete the torrent because
    there is nothing in it to lose.
    """
    if not isinstance(mark, dict):
        return False
    done = int(mark.get("bytes") or 0)
    if done <= 0:
        return False
    return float(mark.get("idle") or 0.0) >= patience(done)


def is_visible(mark: Optional[dict]) -> bool:
    """True once the stall is long enough to be worth telling the user about."""
    if not isinstance(mark, dict) or int(mark.get("bytes") or 0) <= 0:
        return False
    return float(mark.get("idle") or 0.0) >= SHOW_AFTER


def due(mark: Optional[dict], now: float) -> bool:
    """True when it is time to (re)try finding a replacement."""
    if not isinstance(mark, dict):
        return False
    return now >= float(mark.get("retry_at") or 0.0)


def note_try(mark: Optional[dict], now: float) -> dict:
    """Record a rescue attempt and schedule the next one.

    Never gives up: the release may be re-seeded, a new one may be posted, and
    looking costs one indexer query every few hours. It only ever gets rarer.
    """
    m = dict(mark) if isinstance(mark, dict) else {}
    n = int(m.get("tries") or 0)
    m["retry_at"] = float(now) + RETRY_BACKOFF[min(n, len(RETRY_BACKOFF) - 1)]
    m["tries"] = n + 1
    return m
