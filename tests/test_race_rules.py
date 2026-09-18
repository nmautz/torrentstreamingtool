"""Unit tests for `racerules.py` - the decisions that can lose a download.

    python tests/test_race_rules.py      (or `make test`)

These run without qBittorrent, without FastAPI and without a clock: the rules
take plain dicts and an explicit age, which is exactly why they were split out
of main.py. Almost every case here is a way the race could wrongly kill a
candidate that would have won.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import racerules as rr          # noqa: E402

_PASS = 0
_FAIL = []


def ok(name, cond, detail=""):
    global _PASS
    if cond:
        _PASS += 1
    else:
        _FAIL.append("%s%s" % (name, ("\n     " + detail) if detail else ""))


def eq(name, got, want):
    ok(name, got == want, "got %r, want %r" % (got, want))


MB = 1000 ** 2


def entry(rate=0.0, done=0, total=1000 * MB, samples=10, under=5,
          status="live", role="challenger", quality=None):
    return {"status": status, "role": role, "rate_ewma": float(rate),
            "completed": done, "total": total, "samples": samples,
            "under_ratio_ticks": under, "quality": quality or {},
            "hash": "h%d" % id(rate)}


FAST = 4 * MB          # a healthy leader
SLOW = 0.2 * MB        # well under 25% of FAST


# ── entry_progress ───────────────────────────────────────────────────────────
eq("progress of a half-done entry", rr.entry_progress(entry(done=500 * MB)), 0.5)
eq("progress with unknown total is 0", rr.entry_progress(entry(total=0)), 0.0)
eq("progress is clamped", rr.entry_progress(entry(done=9999 * MB)), 1.0)
eq("progress of junk is 0", rr.entry_progress(None), 0.0)


# ── leader ───────────────────────────────────────────────────────────────────
a, b, c = entry(rate=FAST), entry(rate=SLOW), entry(rate=0)
ok("the fastest entry leads", rr.leader([a, b, c]) is a)
ok("no entries -> no leader", rr.leader([]) is None)
ok("THREE DEAD SWARMS ARE NOT A RACE: all-zero -> no leader",
   rr.leader([entry(rate=0), entry(rate=0), entry(rate=0)]) is None)
ok("an entry with bytes but no current rate can still lead",
   rr.leader([entry(rate=0, done=50 * MB), entry(rate=0)]) is not None)
ok("dropped entries are never the leader",
   rr.leader([entry(rate=FAST, status="dropped"), entry(rate=SLOW)])
   is not None and rr.leader(
       [entry(rate=FAST, status="dropped"), entry(rate=SLOW)])["rate_ewma"] == SLOW)
ok("a paused entry is not the leader",
   rr.leader([entry(rate=FAST, status="paused")]) is None)


# ── should_cull: the grace period ────────────────────────────────────────────
lead = entry(rate=FAST, done=300 * MB)
lag = entry(rate=SLOW)
eq("no cull inside the grace period",
   rr.should_cull(lag, lead, 0.0, 3, rr.GRACE_SECS - 1), "")
eq("cull once past it", rr.should_cull(lag, lead, 0.0, 3, rr.GRACE_SECS + 1),
   "outpaced")

eq("no cull without enough samples",
   rr.should_cull(entry(rate=SLOW, samples=rr.MIN_SAMPLES - 1), lead, 0.0, 3, 999),
   "")


# ── should_cull: the leader has to be worth measuring against ────────────────
crawler = entry(rate=rr.MIN_LEADER_BPS - 1, done=300 * MB)
eq("no cull against a crawling leader",
   rr.should_cull(entry(rate=1.0), crawler, 0.0, 3, 999), "")


# ── should_cull: confirmation ticks ──────────────────────────────────────────
eq("one bad tick is not enough",
   rr.should_cull(entry(rate=SLOW, under=rr.CULL_CONFIRM - 1), lead, 0.0, 3, 999),
   "")
eq("sustained is enough",
   rr.should_cull(entry(rate=SLOW, under=rr.CULL_CONFIRM), lead, 0.0, 3, 999),
   "outpaced")


# ── should_cull: bytes beat speed near the end ───────────────────────────────
eq("a 70%-complete laggard is never culled on rate",
   rr.should_cull(entry(rate=SLOW, done=700 * MB), lead, 0.0, 3, 999), "")
eq("...but a 10%-complete one is",
   rr.should_cull(entry(rate=SLOW, done=100 * MB), lead, 0.0, 3, 999), "outpaced")


# ── should_cull: 3 -> 2 is cheap, 2 -> 1 is not ──────────────────────────────
eq("3 -> 2 culls freely", rr.should_cull(lag, lead, SLOW, 3, 999), "outpaced")
eq("2 -> 1 is blocked while the win is ambiguous",
   rr.should_cull(lag, entry(rate=FAST, done=300 * MB), FAST / 2, 2, 999), "")
eq("2 -> 1 goes ahead once the leader is clearly ahead",
   rr.should_cull(lag, entry(rate=FAST, done=300 * MB), SLOW, 2, 999), "outpaced")
eq("2 -> 1 is blocked when the leader has barely started",
   rr.should_cull(lag, entry(rate=FAST, done=1 * MB), SLOW, 2, 999), "")
eq("the last entry standing is never culled",
   rr.should_cull(lag, lead, 0.0, 1, 999), "")


# ── should_cull: the HQ track is exempt from the SPEED rule ──────────────────
eq("an HQ entry is never culled for being slow",
   rr.should_cull(entry(rate=SLOW, role="hq"), lead, 0.0, 3, 999), "")
eq("...but an ordinary entry in the same shape is",
   rr.should_cull(entry(rate=SLOW, role="challenger"), lead, 0.0, 3, 999),
   "outpaced")


# ── should_cull: miscellaneous safety ────────────────────────────────────────
eq("the leader never culls itself", rr.should_cull(lead, lead, 0.0, 3, 999), "")
eq("no leader -> no cull", rr.should_cull(lag, None, 0.0, 3, 999), "")
eq("a non-live entry is not culled again",
   rr.should_cull(entry(rate=SLOW, status="dropped"), lead, 0.0, 3, 999), "")
eq("an entry at exactly the ratio survives",
   rr.should_cull(entry(rate=rr.CULL_RATIO * FAST), lead, 0.0, 3, 999), "")
eq("junk input is never culled", rr.should_cull(None, lead, 0.0, 3, 999), "")


# ── unambiguous ──────────────────────────────────────────────────────────────
ok("a fast, well-advanced leader is unambiguous",
   rr.unambiguous(entry(rate=FAST, done=300 * MB), SLOW))
ok("a leader only marginally ahead is not",
   not rr.unambiguous(entry(rate=FAST, done=300 * MB), FAST / 2))
ok("a fast leader with almost nothing on disk is not",
   not rr.unambiguous(entry(rate=FAST, done=1 * MB), SLOW))
ok("a crawling leader is never unambiguous",
   not rr.unambiguous(entry(rate=100, done=900 * MB), 0.0))
ok("unambiguous tolerates junk", rr.unambiguous(None, 0.0) is False)


# ── leaf guard ───────────────────────────────────────────────────────────────
ok("racerules is a leaf module (no main import)", "main" not in sys.modules)
ok("racerules pulls in no third-party deps",
   not any(m.startswith(("fastapi", "httpx", "pydantic", "uvicorn"))
           for m in sys.modules))


if _FAIL:
    print("FAILED %d of %d" % (len(_FAIL), _PASS + len(_FAIL)))
    for f in _FAIL:
        print("  - " + f)
    sys.exit(1)
print("OK %d/%d" % (_PASS, _PASS))
