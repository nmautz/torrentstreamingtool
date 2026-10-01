"""Unit tests for `stallrule.py` - when a PART-download has stopped for good.

    python tests/test_stallrule.py      (or `make test`)

No qBittorrent, no FastAPI, no clock: the rule takes a mark, a byte count and an
explicit `now`. Almost every case is a way the obvious implementation condemns a
torrent that was fine - or, the bug this exists for, never condemns one that was
not.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import stallrule as sr          # noqa: E402

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


MB = 1024 ** 2
T0 = 1_000_000.0


def run(mark, completed, start, secs, trying=True, step=5.0):
    """Tick the monitor every `step` seconds for `secs`, with no progress."""
    t = start
    while t < start + secs:
        t += step
        mark = sr.advance(mark, completed, t, trying)
    return mark, t


# ── is_trying ────────────────────────────────────────────────────────────────
for s in ("downloading", "stalledDL", "forcedDL"):
    ok("%s is trying" % s, sr.is_trying(s))
for s in ("pausedDL", "stoppedDL", "queuedDL", "checkingDL", "checkingResumeData",
          "moving", "allocating", "metaDL", "uploading", "error", "", None):
    ok("%r is NOT trying" % s, not sr.is_trying(s))


# ── patience ─────────────────────────────────────────────────────────────────
eq("nothing fetched earns the base", sr.patience(0), sr.PATIENCE_BASE)
ok("a 340 MB part-episode is given up on inside 40 minutes",
   sr.PATIENCE_BASE < sr.patience(340 * MB) < 40 * 60)
ok("16 GB on disk earns hours, not minutes", sr.patience(16 * sr.GB) > 5 * 3600)
eq("...but never more than the ceiling", sr.patience(500 * sr.GB), sr.PATIENCE_MAX)
eq("junk reads as nothing fetched", sr.patience("x"), sr.PATIENCE_BASE)


# ── the case this exists for ─────────────────────────────────────────────────
# SpongeBob S01E01 on the box: 337 MB of 404 MB, 0 B/s, twenty-four hours.
m = sr.advance(None, 337 * MB, T0, True)
ok("first sight is not a stall", not sr.is_stuck(m))
eq("first sight starts the idle clock at zero", m["idle"], 0.0)
m, t = run(m, 337 * MB, T0, 10 * 60)
ok("ten idle minutes: not stuck yet", not sr.is_stuck(m))
ok("...but already worth showing", sr.is_visible(m))
m, t = run(m, 337 * MB, t, 30 * 60)
ok("forty idle minutes at 83%: STUCK", sr.is_stuck(m))
ok("and a rescue is due straight away", sr.due(m, t))


# ── progress, however small, resets everything ───────────────────────────────
m2 = sr.note_try(m, t)
ok("a try was recorded", m2["tries"] == 1 and m2["retry_at"] > t)
m3 = sr.advance(m2, 337 * MB + 1, t + 5, True)
ok("one more byte clears the idle clock", m3["idle"] == 0.0 and not sr.is_stuck(m3))
ok("...and the retry history", m3["tries"] == 0 and m3["retry_at"] == 0.0)


# ── zero bytes is the OTHER rule's job ───────────────────────────────────────
z, _ = run(sr.advance(None, 0, T0, True), 0, T0, 12 * 3600)
ok("a zero-byte torrent is never 'stuck' here, however long", not sr.is_stuck(z))
ok("nor shown as a part-download stall", not sr.is_visible(z))


# ── time we were not trying must not count ───────────────────────────────────
p = sr.advance(None, 300 * MB, T0, True)
p, t = run(p, 300 * MB, T0, 12 * 3600, trying=False)
eq("paused / queued / VPN down for 12 h accrues nothing", p["idle"], 0.0)
ok("so it is not stuck", not sr.is_stuck(p))


# ── nor may time we were not WATCHING ────────────────────────────────────────
r = sr.advance(None, 300 * MB, T0, True)
r = sr.advance(r, 300 * MB, T0 + 9 * 3600, True)        # a nine-hour outage
eq("one look after a long gap adds only the tick cap", r["idle"], sr.TICK_CAP)
ok("a restart does not condemn the torrent", not sr.is_stuck(r))
# ...and yet restarts cannot keep it alive for ever, which a timestamp reset on
# boot would. Forty restarts, a minute of observed idle each:
x = sr.advance(None, 300 * MB, T0, True)
t = T0
for _ in range(40):
    t += 3 * 3600                                          # down for hours
    x = sr.advance(x, 300 * MB, t, True)                   # first look after boot
    x, t = run(x, 300 * MB, t, 60)
ok("idle time survives restarts and still adds up", sr.is_stuck(x),
   "idle=%r patience=%r" % (x["idle"], sr.patience(300 * MB)))


# ── fewer bytes than before: re-anchor, don't judge ──────────────────────────
k, t = run(sr.advance(None, 900 * MB, T0, True), 900 * MB, T0, 3600)
ok("an hour idle at 900 MB is stuck", sr.is_stuck(k))
k = sr.note_try(k, t)
k2 = sr.advance(k, 200 * MB, t + 5, True)                  # promoted to a new torrent
ok("a drop in bytes restarts the idle clock", k2["idle"] == 0.0 and not sr.is_stuck(k2))
eq("...on the new byte count", k2["bytes"], 200 * MB)
ok("...without wiping the backoff (it is the same item)", k2["tries"] == 1)


# ── backoff ──────────────────────────────────────────────────────────────────
b = {"bytes": 1, "idle": 1e9, "seen": T0}
waits = []
now = T0
for _ in range(8):
    b = sr.note_try(b, now)
    waits.append(b["retry_at"] - now)
    ok("not due before its time", not sr.due(b, now + waits[-1] - 1))
    ok("due once it has passed", sr.due(b, now + waits[-1]))
    now = b["retry_at"]
ok("retries only ever get rarer", waits == sorted(waits), repr(waits))
eq("the first retry is short (the race cap frees up fast)", waits[0], 10 * 60)
eq("and it settles at the longest wait instead of giving up", waits[-1],
   sr.RETRY_BACKOFF[-1])


# ── junk in ──────────────────────────────────────────────────────────────────
ok("no mark is not stuck", not sr.is_stuck(None))
ok("no mark is not visible", not sr.is_visible("nope"))
ok("no mark is not due", not sr.due(None, T0))
eq("advance tolerates a junk byte count", sr.advance(None, None, T0, True)["bytes"], 0)


print("stallrule: %d passed, %d failed" % (_PASS, len(_FAIL)))
for f in _FAIL:
    print("  FAIL " + f)
sys.exit(1 if _FAIL else 0)
