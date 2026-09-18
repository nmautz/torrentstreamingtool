"""Unit tests for `watchrule.py` - what counts as "watched".

    python tests/test_watchrule.py      (or `make test`)

No server, no clock: every write carries an explicit timestamp. The first block
replays the four false positives measured live against the box in 17.5.0 (a
23:36 Hunter x Hunter episode, no detected credits); each must now stay
unwatched. The rest pin the other direction - real viewing must still complete,
including the ways people actually watch (skipping the intro, 2x, stopping in
the ED) - plus migration of records written before `played_sec` existed.
"""

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import watchrule as wr          # noqa: E402

_PASS = 0
_FAIL = []


def ok(name, cond, detail=""):
    global _PASS
    if cond:
        _PASS += 1
    else:
        _FAIL.append("%s%s" % (name, ("\n     " + detail) if detail else ""))


DUR = 1416.0                    # HxH S01E01, as VLC and the device player report it
T0 = datetime(2026, 9, 18, 10, 0, 0, tzinfo=timezone.utc)


def iso(sec):
    return (T0 + timedelta(seconds=sec)).isoformat(timespec="seconds")


class Session:
    """One file's progress record, driven the way the server sees writes."""

    def __init__(self, rec=None, credits=None, dur=DUR, clock=0.0):
        self.rec, self.credits, self.dur, self.clock = rec, credits, dur, clock

    def write(self, pos, after):
        """A progress write at playhead `pos`, `after` wall seconds after the last."""
        self.clock += after
        at = iso(self.clock)
        played, done = wr.watch_state(self.rec, pos, self.dur, self.credits, at)
        self.rec = {"position_sec": pos, "duration_sec": self.dur,
                    "completed": done, "played_sec": played, "updated_at": at}
        return self

    def play(self, to, rate=1.0, every=15.0):
        """Play from the current position to `to` at `rate`, saving every `every` s."""
        pos = (self.rec or {}).get("position_sec", 0.0)
        while pos < to:
            pos = min(to, pos + every * rate)
            self.write(pos, every)
        return self

    @property
    def done(self):
        return bool(self.rec and self.rec["completed"])

    @property
    def played(self):
        return (self.rec or {}).get("played_sec", 0.0)


# ── The four live false positives - all must stay UNWATCHED ────────────────────

# 1. VLC: ~20 s of play, scrub to 99.6 %, VLC runs out the file (EOF finalise).
s = Session().play(20).write(DUR * 0.996, 3).write(DUR - 1, 6)
ok("VLC scrub to 99.6% then EOF is not watched", not s.done, repr(s.rec))

# 2. Device: play, then scrub so the landing is d-5.7 (asked d-10, HLS overshoots).
s = Session().play(30).write(DUR - 5.7, 2)
ok("device scrub into last 10 s is not watched", not s.done, repr(s.rec))

# 3. Device: scrub past the last segment, element fires `ended` -> POST (d, d).
s = Session().play(30).write(DUR - 5, 2).write(DUR, 1)
ok("device `ended` right after a scrub is not watched", not s.done, repr(s.rec))

# 4. VLC Next 33 s in: the outgoing file is finalised at its real position.
s = Session().play(33)
ok("Next 33 s in is not watched", not s.done)

# A whole sweep of scrubs toward the end, the way t_dev2 walked it.
s = Session().play(20)
for back in (120, 60, 30, 20, 10, 6, 3, 0):
    s.write(DUR - back, 5)
ok("stepwise scrub sweep to the end is not watched", not s.done, repr(s.rec))


# ── Real viewing must still complete ─────────────────────────────────────────

s = Session().play(DUR)
ok("watched straight through completes", s.done, repr(s.rec))

s = Session().play(DUR * 0.899)
ok("stopping just before the tail does not complete", not s.done)
s.play(DUR * 0.905)
ok("…and reaching the tail by playing completes it", s.done, repr(s.rec))

# Skip the 90 s intro early on, watch the rest.
s = Session().play(30).write(120, 2).play(DUR)
ok("skipping the intro still completes", s.done, repr(s.rec))

# The HxH E02 case: watched to 22:01 (93.3 %) and stopped in the ED.
s = Session().play(1321.5)
ok("stopping in the ED at 93 % completes (was stuck unwatched)", s.done, repr(s.rec))

# 2x playback: the playhead moves 30 s per 15 s save.
s = Session().play(DUR, rate=2.0)
ok("2x playback completes", s.done, repr(s.rec))

# Watched across three sittings, days apart.
s = Session().play(500)
s.clock += 3 * 86400
s.write(500, 1).play(1000)
s.clock += 86400
s.write(1000, 1).play(DUR)
ok("watched across several days completes", s.done, repr(s.rec))

# Scrub around a bit mid-episode (back 60 s twice, forward 45 s once).
s = Session().play(400).write(340, 2).play(700).write(640, 2).play(900).write(945, 2).play(DUR)
ok("normal scrubbing mid-episode still completes", s.done, repr(s.rec))


# ── Detected credits move the tail ───────────────────────────────────────────

CS = 1290.0
s = Session(credits=CS).play(CS - 11)
ok("credits: 11 s before credits_start is not watched", not s.done)
s.play(CS - 5)
ok("credits: within the 10 s window completes", s.done, repr(s.rec))

s = Session(credits=CS).play(20).write(CS, 2)
ok("credits: scrubbing straight to the credits is not watched", not s.done)


# ── Stale records and resets ─────────────────────────────────────────────────

# Stopped at 5:00; a week later, open it and drag to the end.
s = Session().play(300)
s.clock += 7 * 86400
s.write(DUR - 2, 1)
ok("a week-old record scrubbed to the end is not watched", not s.done, repr(s.rec))
ok("…and the stale gap paid at most PLAYED_STEP_CAP_SEC",
   s.played <= 300 + wr.PLAYED_STEP_CAP_SEC, repr(s.rec))

# Mark-unwatched writes played_sec 0; a later scrub must not complete off it.
reset = {"position_sec": 0, "duration_sec": DUR, "completed": False,
         "played_sec": 0, "updated_at": iso(0)}
s = Session(rec=reset, clock=3600).write(DUR - 3, 2)
ok("after mark-unwatched, a scrub to the end is not watched", not s.done, repr(s.rec))


# ── First write, monotonic, garbage ──────────────────────────────────────────

played, done = wr.watch_state(None, DUR, DUR, None, iso(0))
ok("first-ever write at the end credits nothing", played == 0.0 and not done)

s = Session().play(DUR)
s.write(12, 30)
ok("completed is monotonic", s.done)

ok("dur 0 never completes", not wr.watch_state({"position_sec": 5, "updated_at": iso(0)},
                                               10, 0, None, iso(15))[1])
p, d = wr.watch_state({"position_sec": float("nan"), "played_sec": "x",
                       "updated_at": "not a date"}, 1400, DUR, None, iso(10))
ok("garbage in a stored record degrades to no credit", p == 0.0 and not d, repr((p, d)))

played, _ = wr.watch_state({"position_sec": 100, "played_sec": 90,
                            "updated_at": iso(60)}, 130, DUR, None, iso(30))
ok("a write timestamped before the last credits nothing", played == 90.0, repr(played))


# ── Records from before played_sec existed ───────────────────────────────────

legacy = {"position_sec": 1321.5, "duration_sec": DUR, "completed": False,
          "updated_at": iso(0)}
s = Session(rec=dict(legacy), clock=0).write(1336.5, 15)
ok("legacy record at 93 % completes on its next write", s.done, repr(s.rec))

legacy = {"position_sec": 300, "duration_sec": DUR, "completed": False,
          "updated_at": iso(0)}
s = Session(rec=dict(legacy), clock=3600).write(DUR - 2, 2)
ok("legacy record at 5:00 scrubbed to the end is not watched", not s.done, repr(s.rec))

s = Session(rec=dict(legacy), clock=3600).write(300, 1).play(DUR)
ok("legacy record at 5:00 finished by playing completes", s.done, repr(s.rec))


# ── Offline batch sync ───────────────────────────────────────────────────────

played, done = wr.offline_watch_state(None, 1400, DUR, None, iso(0))
ok("offline: one event in the tail completes", done and played == 1400.0,
   repr((played, done)))

played, done = wr.offline_watch_state(None, 600, DUR, None, iso(0))
ok("offline: one event mid-episode does not", not done and played == 600.0)
s = Session(rec={"position_sec": 600, "duration_sec": DUR, "completed": False,
                 "played_sec": played, "updated_at": iso(0)}, clock=86400)
s.write(600, 1).play(DUR)
ok("offline half + online half completes", s.done, repr(s.rec))

played, done = wr.offline_watch_state({"position_sec": 900, "played_sec": 1000,
                                       "updated_at": iso(0)}, 200, DUR, None, iso(5))
ok("offline: never lowers credited play", played == 1000.0, repr(played))


# ── Offline sync with device-measured play (iOS OfflineStore, 17.5.0+) ─────────

R = wr.reported_watch_state
played, done = R(None, 1400, DUR, None, 1300)
ok("device: watched offline, reached the tail -> completes", done and played == 1300.0,
   repr((played, done)))
played, done = R(None, 1410, DUR, None, 45)
ok("device: scrubbed to the end offline -> not watched", not done and played == 45.0,
   repr((played, done)))
played, done = R({"position_sec": 700, "played_sec": 900, "updated_at": iso(0)},
                 1300, DUR, None, 300)
ok("device: merged with the host by max, not sum", played == 900.0 and done,
   repr((played, done)))
played, _ = R(None, 1400, DUR, None, 99999)
ok("device: an absurd count is capped at the duration", played == DUR, repr(played))
played, done = R({"position_sec": 600, "duration_sec": DUR, "updated_at": iso(0)},
                 1350, DUR, None, 200)
ok("device: a legacy host record still seeds from its position",
   played == 600.0 and not done, repr((played, done)))
ok("played_of: stored value, legacy position, nothing",
   wr.played_of({"played_sec": 12, "position_sec": 99}) == 12.0
   and wr.played_of({"position_sec": 99}) == 99.0 and wr.played_of(None) == 0.0)


# ── One-shot backfill of pre-17.5.0 stops in the tail ────────────────────────

L = wr.legacy_stopped_in_tail
ok("backfill: HxH E02 stopped at 93 % qualifies",
   L({"position_sec": 1321.5, "duration_sec": DUR, "completed": False}, None))
ok("backfill: stopped at 78 % does not",
   not L({"position_sec": 1115.7, "duration_sec": DUR, "completed": False}, None))
ok("backfill: already completed does not",
   not L({"position_sec": 1321.5, "duration_sec": DUR, "completed": True}, None))
ok("backfill: a record carrying played_sec never does (that's a scrub now)",
   not L({"position_sec": 1321.5, "duration_sec": DUR, "completed": False,
          "played_sec": 40}, None))
ok("backfill: detected credits move the tail (before it)",
   not L({"position_sec": 1270, "duration_sec": DUR, "completed": False}, 1290.0))
ok("backfill: detected credits move the tail (inside it)",
   L({"position_sec": 1285, "duration_sec": DUR, "completed": False}, 1290.0))
ok("backfill: no duration does not",
   not L({"position_sec": 1321.5, "duration_sec": 0, "completed": False}, None))
ok("backfill: position past duration (corrupt) does not",
   not L({"position_sec": DUR + 50, "duration_sec": DUR, "completed": False}, None))
ok("backfill: track-pref-only stub does not",
   not L({"subtitle_sel": {"off": True}}, None))
ok("backfill: non-dict does not", not L(None, None) and not L("x", None))
# The backfill must agree with what the record's next live write would decide.
legacy = {"position_sec": 1321.5, "duration_sec": DUR, "completed": False,
          "updated_at": iso(0)}
_, next_write_done = wr.watch_state(legacy, 1321.5, DUR, None, iso(1))
ok("backfill: agrees with the next write's verdict", L(legacy, None) == next_write_done)


print("%d passed, %d failed" % (_PASS, len(_FAIL)))
for f in _FAIL:
    print("  FAIL", f)
sys.exit(1 if _FAIL else 0)
