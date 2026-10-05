"""Unit tests for `gpugate.py` - is the all-GPU prep path worth trying.

    python tests/test_gpugate.py      (or `make test`)

No ffmpeg and no GPU: the gate only ever sees "that attempt stalled" or "it did
not". The last case replays the night this was written for.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gpugate as gg          # noqa: E402

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


def run(gate, outcomes):
    """Feed one outcome per qualifying encode (True = the GPU attempt would
    stall). Returns 'G' clean GPU, 'S' stalled, '-' attempt skipped."""
    out = ""
    for stalled in outcomes:
        if not gate.allow():
            out += "-"
            continue
        gate.record(stalled)
        out += "S" if stalled else "G"
    return out


# A healthy box never sees the gate.
eq("clean runs stay open", run(gg.Gate(), [False] * 12), "G" * 12)

# One stall is weather, not a pattern.
eq("one stall does not close it", run(gg.Gate(), [True, False, False, False, False]), "SGGGG")
eq("one stall per window never closes it",
   run(gg.Gate(), [True, False, False, False] * 3), "SGGG" * 3)

# Two in the window close it, wherever they fall.
eq("two in a row", run(gg.Gate(), [True, True] + [True] * 3), "SS---")
eq("two with clean runs between", run(gg.Gate(), [True, False, False, True, True]), "SGGS-")

# Closed means exactly REST skips, then exactly one attempt.
g = gg.Gate()
eq("rest length", run(g, [True, True] + [True] * gg.REST), "SS" + "-" * gg.REST)
ok("then it tries again", g.allow())
ok("a stalled probe closes it at once", g.record(True) is True)
eq("and rests in full again", run(g, [False] * (gg.REST + 1)), "-" * gg.REST + "G")

# A clean probe reopens it with a clean slate: the stalls from before the rest
# must not combine with one new stall to close it again.
g = gg.Gate()
run(g, [True, True] + [True] * gg.REST)
eq("clean probe reopens", run(g, [False, True, False, False]), "GSGG")

# An outright ffmpeg error is not a stall (record(False)) and never closes it.
g = gg.Gate()
for _ in range(10):
    g.allow()
    g.record(False)
eq("errors are not stalls", g.state()["resting"], 0)

# record() says when it closed the gate, so the caller can log it once.
g = gg.Gate()
g.allow()
ok("first stall: still open", g.record(True) is False)
g.allow()
ok("second stall: closed", g.record(True) is True)

# 2026-10-04, This Is Us S03E01-E09 on the box: stalled on E01 E03 E05 E06 E07
# E08. Ungated that was six thrown-away attempts; gated it is two.
night = [True, False, True, False, True, True, True, True, False]
got = run(gg.Gate(), night)
eq("the night of 2026-10-04", got, "SGS------")
eq("stalls paid", got.count("S"), 2)

if _FAIL:
    print("FAILED %d of %d:" % (len(_FAIL), _PASS + len(_FAIL)))
    for f in _FAIL:
        print("  - " + f)
    sys.exit(1)
print("gpugate: %d passed" % _PASS)
