"""When may a source file be deleted, now that its bundle can be played without it?

A prepped episode exists on disk twice: the source the torrent downloaded, and
the HLS bundle prep built from it (Original rung + 720p + 480p + AAC per audio
track, so roughly 1.7x the source video -- see docs/STREAMING.md "State +
storage"). The bundle is what every phone, every browser and the TV kiosk
actually play. The source is what VLC plays, what the analyzer fingerprints,
what subsync aligns against, what a repair rewrites, and what a future
`OFFLINE_CACHE_VERSION` bump would rebuild from.

Deleting the source therefore reclaims about 37% of the pair and gives up a
fixed list of capabilities for that file. That trade is only ever worth making
for content nobody is going to touch, and only when the disk actually needs the
room -- which is the whole shape of the policy below:

  * AGE decides what is ELIGIBLE. A series untouched for `idle_days` (or, if
    nothing in it has ever been played, `never_played_days` since it was
    downloaded) joins the candidate pool.
  * FREE SPACE decides what is TAKEN. Nothing is deleted while the disk sits
    above `floor_gb`; below it, the oldest candidates are taken until free space
    is back above `target_gb`, and then it stops.

The aggressive default clock (15 days) is safe precisely because of that split:
a deep candidate pool means the sweep never has to reach for something recent.

**The clock is per SERIES, not per file and not per item.** Touching any episode
of a show protects the whole show, so a half-watched season is never half-evicted
underneath a viewer. Per *item* would not do -- a show downloaded one torrent per
episode is many items, and an item-level clock would age each episode separately,
which is the per-episode behaviour this deliberately rejects.

Split out of `main.py` so the arithmetic can be driven without a server, a disk
or a library: plain values in, a decision out, no I/O and no `main` import.
`tests/test_srcevict.py` drives it. `main.py` gathers the facts (stat, qBittorrent
completion, bundle verdicts, per-profile progress) and wires them in.

See docs/STREAMING.md "Source eviction" and docs/GOTCHAS.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Optional, Sequence

GIB = 1024 ** 3

# A series untouched this long is eligible. Deliberately short: eligibility is not
# deletion (the free-space floor is what actually pulls the trigger), and a deep
# pool is what lets the sweep always take the oldest thing rather than the newest
# thing it is allowed to.
DEFAULT_IDLE_DAYS = 15
# ...but a series nothing has EVER played ages from its download date on a shorter
# clock. Downloaded weeks ago and never opened is the strongest signal in the
# library that a source is dead weight.
DEFAULT_NEVER_PLAYED_DAYS = 7
# Free space below this (GiB) starts a sweep; the sweep stops once free space is
# back above DEFAULT_TARGET_GB. Two separate numbers, so a disk hovering at the
# line doesn't delete one file every tick forever.
DEFAULT_FLOOR_GB = 100.0
DEFAULT_TARGET_GB = 150.0

IDLE_DAYS_RANGE = (1, 3650)
GB_RANGE = (1.0, 1_000_000.0)


# Why a file was passed over. Every one is surfaced in the dry run, because "it
# would free 0 bytes" is useless without "...and here is what is holding each
# file back". Ordered roughly cheapest-to-establish first.
BLOCK_NOT_VIDEO = "not-video"              # not a video file; nothing prepped it
BLOCK_ALREADY_EVICTED = "already-evicted"  # source already reclaimed by a previous sweep
BLOCK_NO_SOURCE = "no-source"              # no source on disk and no eviction record vouching for it
BLOCK_NOT_AGED = "not-aged"                # series touched inside the clock
BLOCK_NO_BUNDLE = "no-bundle"              # nothing to play once the source goes
BLOCK_STALE_VERSION = "stale-version"      # bundle predates the current format
BLOCK_UNVERIFIED = "unverified"            # no current clean bundle_check verdict
BLOCK_DAMAGED = "damaged"                  # bundle_check says the bundle is dead
BLOCK_INCOMPLETE_BUNDLE = "incomplete-bundle"   # a playlist references a missing/empty segment
BLOCK_SOURCE_INCOMPLETE = "source-incomplete"   # torrent isn't 100% / recheck not clean
BLOCK_IN_PROGRESS = "in-progress"          # somebody is part-way through it
BLOCK_NEXT_UP = "next-up"                  # it is somebody's next episode
BLOCK_BUSY = "busy"                        # prepping, compressing, racing, playing

# Presentation order for the dry run's blocker summary: the ones an operator can
# act on come first.
BLOCKER_ORDER = (
    BLOCK_NOT_AGED, BLOCK_NO_BUNDLE, BLOCK_UNVERIFIED, BLOCK_DAMAGED,
    BLOCK_INCOMPLETE_BUNDLE, BLOCK_SOURCE_INCOMPLETE, BLOCK_STALE_VERSION,
    BLOCK_IN_PROGRESS, BLOCK_NEXT_UP, BLOCK_BUSY, BLOCK_ALREADY_EVICTED,
    BLOCK_NO_SOURCE, BLOCK_NOT_VIDEO,
)


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


@dataclass(frozen=True)
class Policy:
    """The tunables, already clamped. Build with `policy_from()`."""
    enabled: bool = False
    idle_days: int = DEFAULT_IDLE_DAYS
    never_played_days: int = DEFAULT_NEVER_PLAYED_DAYS
    floor_gb: float = DEFAULT_FLOOR_GB
    target_gb: float = DEFAULT_TARGET_GB

    @property
    def floor_bytes(self) -> int:
        return int(self.floor_gb * GIB)

    @property
    def target_bytes(self) -> int:
        return int(self.target_gb * GIB)

    def days_for(self, ever_played: bool) -> int:
        return self.idle_days if ever_played else self.never_played_days


def policy_from(cfg: Optional[dict]) -> Policy:
    """A Policy from raw settings, with every field clamped to a sane range.

    Garbage in any field falls back to that field's default rather than raising --
    this reads persisted JSON that a human may have hand-edited, and a typo in
    `target_gb` must not take the maintenance loop down.
    """
    cfg = cfg if isinstance(cfg, dict) else {}

    def num(key, default, lo, hi, cast):
        try:
            return cast(_clamp(float(cfg.get(key, default)), lo, hi))
        except (TypeError, ValueError):
            return cast(default)

    idle = num("idle_days", DEFAULT_IDLE_DAYS, *IDLE_DAYS_RANGE, int)
    never = num("never_played_days", DEFAULT_NEVER_PLAYED_DAYS, *IDLE_DAYS_RANGE, int)
    floor = num("floor_gb", DEFAULT_FLOOR_GB, *GB_RANGE, float)
    target = num("target_gb", DEFAULT_TARGET_GB, *GB_RANGE, float)
    # A target at or below the floor would sweep one file per tick forever: the
    # floor keeps firing and the target is already met. Force real hysteresis.
    if target <= floor:
        target = floor * 1.25
    return Policy(
        enabled=bool(cfg.get("enabled", False)),
        idle_days=idle, never_played_days=never,
        floor_gb=floor, target_gb=target,
    )


@dataclass(frozen=True)
class SeriesClock:
    """When a series was last touched, and whether that is a play or a download."""
    at: Optional[datetime]
    ever_played: bool

    def days_idle(self, now: datetime) -> float:
        if self.at is None:
            return float("inf")
        return max(0.0, (now - self.at).total_seconds() / 86400.0)


def series_clock(touched_at: Iterable[Optional[datetime]],
                 added_at: Iterable[Optional[datetime]]) -> SeriesClock:
    """The series' clock: the newest progress write across every profile and every
    item in it, else the newest download date among those items.

    `touched_at` is every `file_progress[*].updated_at` in the series, from every
    profile -- one person still watching a show protects it for everyone. A series
    with no progress record anywhere has never been played and falls back to
    `added_at`, which then ages on the shorter clock.

    The NEWEST added_at is the fallback, not the oldest: a show still receiving
    episodes is live content even if nobody has started it.
    """
    played = [t for t in touched_at if t is not None]
    if played:
        return SeriesClock(max(played), True)
    added = [t for t in added_at if t is not None]
    return SeriesClock(max(added) if added else None, False)


def is_aged(clock: SeriesClock, now: datetime, policy: Policy) -> bool:
    """Has this series been quiet long enough to enter the candidate pool?

    A series with no clock at all (no progress anywhere, no parseable added_at) is
    NOT aged. An unreadable timestamp is missing evidence, and missing evidence
    must never be read as "safe to delete".
    """
    if clock.at is None:
        return False
    return clock.days_idle(now) >= policy.days_for(clock.ever_played)


@dataclass(frozen=True)
class Candidate:
    """One library file weighed for eviction. `main.py` builds these; everything
    here treats them as already-established facts.

    `source_bytes` is what deleting actually frees -- the source file alone. The
    bundle stays, so it is not counted.
    """
    path: str
    series_key: str
    name: str = ""
    item_id: str = ""
    item_title: str = ""
    source_bytes: int = 0
    clock: SeriesClock = field(default_factory=lambda: SeriesClock(None, False))
    blockers: tuple = ()

    @property
    def eligible(self) -> bool:
        return not self.blockers


def block(candidate: Candidate, *reasons: str) -> Candidate:
    """A copy of `candidate` carrying the extra blockers, de-duplicated and in
    BLOCKER_ORDER so two files blocked for the same reasons compare equal."""
    merged = set(candidate.blockers) | {r for r in reasons if r}
    ordered = tuple(r for r in BLOCKER_ORDER if r in merged)
    extra = tuple(sorted(r for r in merged if r not in BLOCKER_ORDER))
    return Candidate(
        path=candidate.path, series_key=candidate.series_key, name=candidate.name,
        item_id=candidate.item_id, item_title=candidate.item_title,
        source_bytes=candidate.source_bytes, clock=candidate.clock,
        blockers=ordered + extra,
    )


def age_blockers(clock: SeriesClock, now: datetime, policy: Policy) -> tuple:
    """The age half of the gate, as a blocker tuple (empty when aged)."""
    return () if is_aged(clock, now, policy) else (BLOCK_NOT_AGED,)


def _order_key(c: Candidate):
    """Oldest series first; inside one series, biggest file first.

    Every file in a series shares one clock, so the tie-break decides most of the
    real ordering. Biggest-first means a given deficit is met by taking the fewest
    files -- the least number of episodes that lose VLC, 5.1 and repairability.
    `path` last so the plan is deterministic across runs and platforms.
    """
    ts = c.clock.at.timestamp() if c.clock.at is not None else float("-inf")
    return (ts, -c.source_bytes, c.path)


@dataclass(frozen=True)
class Plan:
    """What a sweep would do right now.

    `would_delete` is empty whenever `triggered` is False -- above the floor there
    is nothing to do, however many candidates are eligible. That is the difference
    between eligible and taken, and the dry run reports both.
    """
    triggered: bool
    free_bytes: int
    floor_bytes: int
    target_bytes: int
    deficit_bytes: int
    would_delete: tuple = ()
    would_free_bytes: int = 0
    eligible: tuple = ()
    blocked: tuple = ()
    met: bool = False

    @property
    def eligible_bytes(self) -> int:
        return sum(c.source_bytes for c in self.eligible)

    @property
    def shortfall_bytes(self) -> int:
        """How much of the deficit the eligible pool cannot cover."""
        return max(0, self.deficit_bytes - self.would_free_bytes)


def plan(candidates: Sequence[Candidate], free_bytes: int, policy: Policy) -> Plan:
    """Decide the sweep. Pure: the same inputs always give the same plan.

    Above the floor nothing is taken, and the plan still reports the whole
    eligible pool so the dry run can answer "what COULD this free?" without
    pretending the sweep would run.
    """
    eligible = tuple(sorted((c for c in candidates if c.eligible), key=_order_key))
    blocked = tuple(c for c in candidates if not c.eligible)
    floor_b, target_b = policy.floor_bytes, policy.target_bytes
    triggered = bool(policy.enabled) and free_bytes < floor_b
    deficit = max(0, target_b - free_bytes) if triggered else 0

    take: list = []
    freed = 0
    if triggered:
        for c in eligible:
            if freed >= deficit:
                break
            if c.source_bytes <= 0:
                continue          # nothing to reclaim; never spend a deletion on it
            take.append(c)
            freed += c.source_bytes

    return Plan(
        triggered=triggered, free_bytes=int(free_bytes),
        floor_bytes=floor_b, target_bytes=target_b, deficit_bytes=deficit,
        would_delete=tuple(take), would_free_bytes=freed,
        eligible=eligible, blocked=blocked,
        met=bool(triggered and freed >= deficit),
    )


def blocker_summary(candidates: Sequence[Candidate]) -> list:
    """`[(reason, file_count, bytes)]` in BLOCKER_ORDER, for the dry-run readout.

    A file counts once per blocker it carries, so the numbers explain every file
    rather than only its first problem -- "412 not aged" and "9 damaged" are
    different actions, and a file can want both.
    """
    counts: dict = {}
    for c in candidates:
        for r in c.blockers:
            n, b = counts.get(r, (0, 0))
            counts[r] = (n + 1, b + c.source_bytes)
    known = [(r, *counts[r]) for r in BLOCKER_ORDER if r in counts]
    rest = sorted((r, *v) for r, v in counts.items() if r not in BLOCKER_ORDER)
    return [(r, n, b) for (r, n, b) in known + rest]


def sole_blocker_summary(candidates: Sequence[Candidate]) -> list:
    """`[(reason, file_count, bytes)]` counting only files where that reason is the
    **one and only** thing stopping them.

    This is the actionable half of the readout, and it answers a question the plain
    summary cannot: *if this single obstacle cleared, how much would open up?* A
    file blocked by both `not-aged` and `unverified` is not waiting on the bundle
    audit -- it is waiting on time -- so counting it under `unverified` would
    promise a pool that finishing the audit could not deliver.
    """
    counts: dict = {}
    for c in candidates:
        if len(c.blockers) != 1:
            continue
        r = c.blockers[0]
        n, b = counts.get(r, (0, 0))
        counts[r] = (n + 1, b + c.source_bytes)
    known = [(r, *counts[r]) for r in BLOCKER_ORDER if r in counts]
    rest = sorted((r, *v) for r, v in counts.items() if r not in BLOCKER_ORDER)
    return [(r, n, b) for (r, n, b) in known + rest]


def parse_iso(value) -> Optional[datetime]:
    """An ISO-8601 stamp as an aware UTC datetime, or None.

    Tolerant on purpose: these come from `library.json`, where an offline-sync
    write can carry a non-UTC offset and an old record can carry a trailing "Z"
    that `fromisoformat` refuses before 3.11. A naive stamp is read as UTC, which
    is what every writer in this codebase means by one.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
