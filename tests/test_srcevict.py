"""Unit tests for `srcevict.py` - when a source file may be deleted.

    python tests/test_srcevict.py       (or `make test`)

No server, no disk, no clock: every timestamp is explicit. The tests pin the two
halves of the policy separately, because conflating them is the mistake the whole
design exists to avoid - AGE decides what is eligible, FREE SPACE decides what is
actually taken, and a full disk must never be able to reach something recent.
"""

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import srcevict as se           # noqa: E402

_PASS = 0
_FAIL = []


def ok(name, cond, detail=""):
    global _PASS
    if cond:
        _PASS += 1
    else:
        _FAIL.append("%s%s" % (name, ("\n     " + detail) if detail else ""))


NOW = datetime(2026, 9, 19, 12, 0, 0, tzinfo=timezone.utc)
GIB = se.GIB


def ago(days):
    return NOW - timedelta(days=days)


def pol(**kw):
    base = {"enabled": True, "idle_days": 15, "never_played_days": 7,
            "floor_gb": 100, "target_gb": 150}
    base.update(kw)
    return se.policy_from(base)


def cand(path, series, days, *, played=True, gb=1.0, blockers=()):
    return se.Candidate(
        path=path, series_key=series, name=os.path.basename(path),
        source_bytes=int(gb * GIB),
        clock=se.SeriesClock(ago(days), played),
        blockers=tuple(blockers),
    )


# ── policy_from: clamping and hysteresis ──────────────────────────────────────
P = se.policy_from({})
ok("defaults: disabled out of the box", P.enabled is False)
ok("defaults: 15 / 7 day clocks",
   P.idle_days == se.DEFAULT_IDLE_DAYS and P.never_played_days == se.DEFAULT_NEVER_PLAYED_DAYS)
ok("policy: garbage falls back to the default, does not raise",
   se.policy_from({"idle_days": "soon", "floor_gb": None}).idle_days == 15)
ok("policy: idle_days clamped up from 0", se.policy_from({"idle_days": 0}).idle_days == 1)
ok("policy: idle_days clamped down from absurd",
   se.policy_from({"idle_days": 99999}).idle_days == se.IDLE_DAYS_RANGE[1])
ok("policy: non-dict config is survivable", se.policy_from(None).idle_days == 15)
# A target at or under the floor would fire every tick forever: floor breached,
# target already met, one file taken, repeat. Hysteresis is forced.
ok("policy: target below floor is pushed above it",
   se.policy_from({"floor_gb": 100, "target_gb": 50}).target_bytes > se.policy_from({"floor_gb": 100}).floor_bytes)
ok("policy: target equal to floor is pushed above it",
   se.policy_from({"floor_gb": 100, "target_gb": 100}).target_gb > 100)
ok("policy: days_for picks the clock by play history",
   pol().days_for(True) == 15 and pol().days_for(False) == 7)

# ── series_clock: the rollup ──────────────────────────────────────────────────
c = se.series_clock([ago(40), ago(3), None], [ago(100)])
ok("clock: newest play across profiles wins", c.ever_played and c.at == ago(3))
ok("clock: a play beats an older download", c.at == ago(3))
c = se.series_clock([], [ago(30), ago(5)])
ok("clock: never played falls back to download date", (not c.ever_played) and c.at == ago(5))
ok("clock: NEWEST download wins - a show still receiving episodes is live",
   se.series_clock([], [ago(300), ago(2)]).at == ago(2))
ok("clock: nothing at all yields no clock", se.series_clock([], []).at is None)
ok("clock: None entries are ignored, not treated as now",
   se.series_clock([None, None], [None]).at is None)
ok("clock: days_idle measures from the stamp",
   abs(se.SeriesClock(ago(9), True).days_idle(NOW) - 9.0) < 1e-6)
ok("clock: a future stamp reads as 0 days idle, never negative",
   se.SeriesClock(NOW + timedelta(days=5), True).days_idle(NOW) == 0.0)

# ── is_aged: the two clocks ───────────────────────────────────────────────────
ok("aged: played 20d ago is past the 15d clock", se.is_aged(se.SeriesClock(ago(20), True), NOW, pol()))
ok("aged: played 14d ago is not", not se.is_aged(se.SeriesClock(ago(14), True), NOW, pol()))
ok("aged: exactly at the clock counts", se.is_aged(se.SeriesClock(ago(15), True), NOW, pol()))
ok("aged: never-played 8d old is past the SHORTER 7d clock",
   se.is_aged(se.SeriesClock(ago(8), False), NOW, pol()))
ok("aged: never-played 6d old is not", not se.is_aged(se.SeriesClock(ago(6), False), NOW, pol()))
# The same 10-day-old series is eligible if nobody ever played it and safe if
# somebody did. That asymmetry is the whole point of the second clock.
ok("aged: 10d splits on play history",
   se.is_aged(se.SeriesClock(ago(10), False), NOW, pol())
   and not se.is_aged(se.SeriesClock(ago(10), True), NOW, pol()))
# Missing evidence is never read as "safe to delete".
ok("aged: no clock is NOT aged", not se.is_aged(se.SeriesClock(None, False), NOW, pol()))
ok("age_blockers: aged yields an empty tuple",
   se.age_blockers(se.SeriesClock(ago(30), True), NOW, pol()) == ())
ok("age_blockers: fresh yields not-aged",
   se.age_blockers(se.SeriesClock(ago(1), True), NOW, pol()) == (se.BLOCK_NOT_AGED,))

# ── block(): merging reasons ──────────────────────────────────────────────────
b = se.block(cand("/a.mkv", "s", 30), se.BLOCK_DAMAGED, se.BLOCK_NOT_AGED)
ok("block: reasons land in BLOCKER_ORDER, not call order",
   b.blockers == (se.BLOCK_NOT_AGED, se.BLOCK_DAMAGED))
ok("block: de-duplicates",
   se.block(b, se.BLOCK_DAMAGED).blockers == (se.BLOCK_NOT_AGED, se.BLOCK_DAMAGED))
ok("block: a blocked candidate is not eligible", not b.eligible)
ok("block: empty reason strings are dropped",
   se.block(cand("/a.mkv", "s", 30), "").blockers == ())
ok("block: preserves the payload",
   b.source_bytes == int(1.0 * GIB) and b.series_key == "s")
ok("candidate: no blockers means eligible", cand("/a.mkv", "s", 30).eligible)

# ── plan: the floor gate ──────────────────────────────────────────────────────
pool = [cand("/old1.mkv", "alpha", 90, gb=4),
        cand("/old2.mkv", "alpha", 90, gb=2),
        cand("/mid.mkv", "beta", 40, gb=8),
        cand("/fresh.mkv", "gamma", 2, blockers=(se.BLOCK_NOT_AGED,), gb=50)]

p = se.plan(pool, free_bytes=int(400 * GIB), policy=pol())
ok("plan: above the floor nothing is taken", p.triggered is False and p.would_delete == ())
ok("plan: ...but the eligible pool is still reported", len(p.eligible) == 3)
ok("plan: eligible_bytes sums the pool that COULD be freed",
   p.eligible_bytes == int(14 * GIB))
ok("plan: no deficit above the floor", p.deficit_bytes == 0)

p = se.plan(pool, free_bytes=int(400 * GIB), policy=pol(enabled=False))
ok("plan: disabled never triggers", p.triggered is False)
p = se.plan(pool, free_bytes=int(10 * GIB), policy=pol(enabled=False))
ok("plan: disabled does not trigger even on a full disk",
   p.triggered is False and p.would_delete == ())
ok("plan: disabled still reports the pool, so the dry run works while off",
   len(p.eligible) == 3)

# ── plan: below the floor, oldest first, stop at the target ───────────────────
# Free 95 GiB, floor 100, target 150 -> deficit 55 GiB.
p = se.plan(pool, free_bytes=int(95 * GIB), policy=pol())
ok("plan: below the floor it triggers", p.triggered)
ok("plan: deficit is measured to the TARGET, not the floor",
   p.deficit_bytes == int(55 * GIB))
ok("plan: the pool cannot cover it, so everything eligible is taken",
   len(p.would_delete) == 3 and p.would_free_bytes == int(14 * GIB))
ok("plan: met is False when the pool falls short", p.met is False)
ok("plan: shortfall names the gap", p.shortfall_bytes == int(41 * GIB))
ok("plan: blocked files are never taken, however full the disk",
   all(c.path != "/fresh.mkv" for c in p.would_delete))

# The smallest deficit a trigger can produce is target-floor, since free must be
# under the floor to fire at all -- so a narrow pair is how you test "stops early".
# Free 99, floor 100, target 103 -> deficit 4 GiB, met by the single 4 GiB file.
narrow = pol(floor_gb=100, target_gb=103)
p = se.plan(pool, free_bytes=int(99 * GIB), policy=narrow)
ok("plan: small deficit takes only what it needs", len(p.would_delete) == 1)
ok("plan: oldest series first", p.would_delete[0].series_key == "alpha")
ok("plan: inside a series, biggest file first (fewest episodes lose their source)",
   p.would_delete[0].path == "/old1.mkv")
ok("plan: met once the target is reached", p.met is True)
ok("plan: it stops - the 8 GiB beta file is untouched though it would also fit",
   all(c.path != "/mid.mkv" for c in p.would_delete))

# Ordering across series must follow the clock, not the size.
order = se.plan([cand("/b.mkv", "beta", 20, gb=100), cand("/a.mkv", "alpha", 900, gb=1)],
                free_bytes=0, policy=pol()).would_delete
ok("plan: a tiny ancient file is taken before a huge recent one",
   order[0].path == "/a.mkv")

# A zero-byte source frees nothing; spending a deletion on it is pure loss.
p = se.plan([cand("/empty.mkv", "z", 99, gb=0), cand("/real.mkv", "z", 99, gb=3)],
            free_bytes=int(99 * GIB), policy=narrow)
ok("plan: a zero-byte source is skipped", all(c.path != "/empty.mkv" for c in p.would_delete))
ok("plan: ...and the real file is still taken", len(p.would_delete) == 1)

ok("plan: an empty library is survivable",
   se.plan([], free_bytes=0, policy=pol()).would_delete == ())
ok("plan: determinism - the same inputs give the same plan",
   [c.path for c in se.plan(pool, int(95 * GIB), pol()).would_delete]
   == [c.path for c in se.plan(list(reversed(pool)), int(95 * GIB), pol()).would_delete])

# ── blocker_summary ───────────────────────────────────────────────────────────
summary = se.blocker_summary([
    se.block(cand("/a.mkv", "s", 1, gb=2), se.BLOCK_NOT_AGED),
    se.block(cand("/b.mkv", "s", 1, gb=3), se.BLOCK_NOT_AGED, se.BLOCK_DAMAGED),
    cand("/c.mkv", "s", 99, gb=9),
])
as_dict = {r: (n, b) for (r, n, b) in summary}
ok("summary: counts every file carrying a reason", as_dict[se.BLOCK_NOT_AGED][0] == 2)
ok("summary: sums their bytes", as_dict[se.BLOCK_NOT_AGED][1] == int(5 * GIB))
ok("summary: a file counts under EACH of its blockers", as_dict[se.BLOCK_DAMAGED][0] == 1)
ok("summary: eligible files appear nowhere", sum(n for (_, n, _) in summary) == 3)
ok("summary: follows BLOCKER_ORDER",
   [r for (r, _, _) in summary] == [se.BLOCK_NOT_AGED, se.BLOCK_DAMAGED])
ok("summary: empty in, empty out", se.blocker_summary([]) == [])

# ── sole_blocker_summary ──────────────────────────────────────────────────────
pool2 = [
    se.block(cand("/a.mkv", "s", 1, gb=2), se.BLOCK_UNVERIFIED),                    # sole
    se.block(cand("/b.mkv", "s", 1, gb=3), se.BLOCK_UNVERIFIED),                    # sole
    se.block(cand("/c.mkv", "s", 1, gb=9), se.BLOCK_UNVERIFIED, se.BLOCK_NOT_AGED), # not sole
    se.block(cand("/d.mkv", "s", 1, gb=4), se.BLOCK_NOT_AGED),                      # sole
    cand("/e.mkv", "s", 99, gb=7),                                                  # eligible
]
sole = {r: (n, b) for (r, n, b) in se.sole_blocker_summary(pool2)}
ok("sole: counts only files with exactly one blocker", sole[se.BLOCK_UNVERIFIED][0] == 2)
ok("sole: sums their bytes", sole[se.BLOCK_UNVERIFIED][1] == int(5 * GIB))
# The whole point: a file ALSO waiting on the clock is not waiting on the audit,
# so finishing the audit cannot deliver it. Counting it would overpromise the pool.
ok("sole: a multi-blocker file is excluded from every reason it carries",
   sole[se.BLOCK_NOT_AGED] == (1, int(4 * GIB))          # d only; c is excluded
   and sole[se.BLOCK_UNVERIFIED] == (2, int(5 * GIB)),   # a+b only; c is excluded
   "c.mkv (9 GiB, unverified AND not-aged) must not be counted under either")
ok("sole: eligible files appear nowhere", sum(n for (_, n, _) in se.sole_blocker_summary(pool2)) == 3)
ok("sole: never exceeds the plain summary for the same reason",
   all(sole.get(r, (0, 0))[0] <= n
       for (r, n, _) in se.blocker_summary(pool2)))
ok("sole: empty in, empty out", se.sole_blocker_summary([]) == [])
ok("sole: follows BLOCKER_ORDER",
   [r for (r, _, _) in se.sole_blocker_summary(pool2)] == [se.BLOCK_NOT_AGED, se.BLOCK_UNVERIFIED])

# ── no-source is its own reason ───────────────────────────────────────────────
# A file the sweep never touched must never be reported as "source already
# reclaimed" — that reads as eviction having run when it has not.
ok("no-source is distinct from already-evicted", se.BLOCK_NO_SOURCE != se.BLOCK_ALREADY_EVICTED)
ok("no-source is in BLOCKER_ORDER", se.BLOCK_NO_SOURCE in se.BLOCKER_ORDER)

# ── parse_iso ─────────────────────────────────────────────────────────────────
ok("iso: trailing Z is accepted (3.9-safe)",
   se.parse_iso("2026-09-19T12:00:00Z") == NOW)
ok("iso: explicit offset is normalised",
   se.parse_iso("2026-09-19T13:00:00+01:00") == NOW)
ok("iso: a naive stamp is read as UTC", se.parse_iso("2026-09-19T12:00:00") == NOW)
ok("iso: a datetime passes through", se.parse_iso(NOW) == NOW)
ok("iso: a naive datetime is given UTC",
   se.parse_iso(datetime(2026, 9, 19, 12, 0, 0)) == NOW)
ok("iso: junk yields None", se.parse_iso("not a date") is None)
ok("iso: empty / None / wrong type yield None",
   se.parse_iso("") is None and se.parse_iso(None) is None and se.parse_iso(42) is None)

# ── the property that matters most ────────────────────────────────────────────
# However full the disk gets, a series touched inside its clock is untouchable.
recent = [se.block(cand("/watching-s0%de01.mkv" % i, "hot", 0), se.BLOCK_NOT_AGED)
          for i in range(1, 6)]
p = se.plan(recent, free_bytes=0, policy=pol(floor_gb=1000, target_gb=5000))
ok("INVARIANT: a zero-free disk still cannot touch a recently-watched series",
   p.triggered and p.would_delete == () and p.shortfall_bytes == p.deficit_bytes)

print("%d passed, %d failed" % (_PASS, len(_FAIL)))
for f in _FAIL:
    print("  FAIL", f)
sys.exit(1 if _FAIL else 0)
