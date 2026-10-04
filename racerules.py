"""The arithmetic behind a download race: who is winning, and who to drop.

Split out of `main.py` for one reason - these are the decisions that can lose a
download, and they are the only part of the race engine that can be tested
without a running qBittorrent. Everything here is pure: plain dicts in, a
verdict out, no clock (ages are passed in), no I/O, no `main` import.
`tests/test_race_rules.py` exercises it directly.

The engine in `main.py` owns everything else - sampling qBit, adding and
deleting torrents, promotion, the HQ upgrade swap.

Every clause in `should_cull` exists to stop the race killing a candidate that
would have won, so the whole file is deliberately conservative: when in doubt,
keep the torrent. The cost of keeping a loser one extra tick is five seconds of
bandwidth; the cost of dropping a winner is starting its download again from
zero.

See docs/GOTCHAS.md and docs/BACKEND.md.
"""

from __future__ import annotations

from typing import Any, Optional

import relquality

# No entry is culled on speed before this much wall-clock has passed. DHT
# resolution plus a first peer handshake is routinely 30-60 s over a VPN, and a
# candidate judged inside that window is being judged on connection latency
# rather than on throughput.
GRACE_SECS = 90.0
# ...and not before we have enough samples for the EWMA to mean anything.
MIN_SAMPLES = 3
# Below this fraction of the leader's rate, a candidate is losing.
CULL_RATIO = 0.25
# Consecutive ticks under that ratio before it is actually dropped. The counter
# resets on any tick that is NOT under the ratio, so a momentary dip - a piece
# batch boundary, a tracker re-announce - never accumulates into a kill.
CULL_CONFIRM = 3
# Never cull against a leader that is itself crawling. If the fastest candidate
# manages 80 KB/s, "a quarter of the leader" is inside the noise floor and the
# slower one may simply not have found its peers yet.
MIN_LEADER_BPS = 300_000
# Past this much of itself, an entry's BYTES are worth more than its rate: a
# torrent at 70% that has gone slow will still beat a fast one starting at zero.
KEEP_PROGRESS = 0.60
# The 3-to-2 cull is cheap. The 2-to-1 cull throws away the last spare, so the
# leader has to be ahead by this multiple before it is allowed.
UNAMBIGUOUS_RATIO = 4.0
# ...and have enough of the file to prove the rate was not a burst.
UNAMBIGUOUS_PROGRESS = 0.25


def entry_progress(e: dict) -> float:
    """Fraction of this candidate that is on disk, 0.0 when its size is unknown."""
    if not isinstance(e, dict):
        return 0.0
    try:
        total = float(e.get("total") or 0)
        done = float(e.get("completed") or 0)
    except (TypeError, ValueError):
        return 0.0
    if total <= 0:
        return 0.0
    return max(0.0, min(1.0, done / total))


def leader(entries: Any) -> Optional[dict]:
    """The candidate currently winning, or None when nothing is actually moving.

    Ranked on the smoothed byte rate first, bytes fetched second, and release
    quality only as a final tie-break.

    **If nothing has fetched anything, there is no leader.** Three dead swarms
    are not a race, and naming a "winner" among them would hand `should_cull` a
    baseline of zero to measure the others against - which every one of them
    would fail, killing the whole field on rounding noise.
    """
    if not entries:
        return None
    live = [e for e in entries
            if isinstance(e, dict) and e.get("status") == "live"]
    if not live:
        return None
    if not any(float(e.get("rate_ewma") or 0) > 0 or float(e.get("completed") or 0) > 0
               for e in live):
        return None
    return max(live, key=lambda e: (float(e.get("rate_ewma") or 0.0),
                                    float(e.get("completed") or 0),
                                    relquality.rank_key(e.get("quality") or {})))


def unambiguous(lead: dict, second_rate: float) -> bool:
    """True when the leader has won clearly enough to discard the last spare."""
    if not isinstance(lead, dict):
        return False
    lr = float(lead.get("rate_ewma") or 0.0)
    if lr < MIN_LEADER_BPS:
        return False
    if second_rate > 0 and lr < UNAMBIGUOUS_RATIO * second_rate:
        return False
    return entry_progress(lead) >= UNAMBIGUOUS_PROGRESS


def should_cull(entry: dict, lead: Optional[dict], second_rate: float,
                live_count: int, age_secs: float) -> str:
    """A reason to drop `entry` for being outpaced, or "" to keep it.

    `age_secs` is passed in rather than read off a clock so this is directly
    testable and so the caller can measure age in persisted wall-clock (a race
    has to survive the restarts this box does routinely).

    Note what is NOT here: being dead, fake or metadata-less. Those are facts
    rather than judgements, they need no grace period, and the engine handles
    them before it ever gets this far. This function only ever answers "is this
    one losing a race it could still win?".
    """
    if not isinstance(entry, dict) or lead is None or entry is lead:
        return ""
    if entry.get("status") != "live":
        return ""
    # The HQ track is exempt from the SPEED predicate - being slower is the
    # entire premise of the two-track. It is still dropped for being dead or
    # hopeless, which the engine decides, not this function.
    if entry.get("role") == "hq":
        return ""
    if live_count <= 1:
        return ""                      # never cull the last one standing
    if age_secs < GRACE_SECS:
        return ""
    if int(entry.get("samples") or 0) < MIN_SAMPLES:
        return ""
    lead_rate = float(lead.get("rate_ewma") or 0.0)
    if lead_rate < MIN_LEADER_BPS:
        return ""
    if float(entry.get("rate_ewma") or 0.0) >= CULL_RATIO * lead_rate:
        return ""
    if int(entry.get("under_ratio_ticks") or 0) < CULL_CONFIRM:
        return ""
    if entry_progress(entry) >= KEEP_PROGRESS:
        return ""                      # its bytes now outweigh its speed
    if live_count <= 2 and not unambiguous(lead, second_rate):
        return ""                      # keep a viable spare until it is settled
    return "outpaced"
