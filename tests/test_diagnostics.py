"""Unit tests for the loop-block sampler in `diagnostics.py`.

    python tests/test_diagnostics.py      (or `make test`)

The stall dump is written by the event loop itself, so it only ever shows the
aftermath of a freeze. The sampler watches the loop from another thread and
must name the frame that was blocking it. Stdlib only; about four seconds.
"""

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import diagnostics as diag      # noqa: E402

_PASS = 0
_FAIL = []


def ok(name, cond, detail=""):
    global _PASS
    if cond:
        _PASS += 1
    else:
        _FAIL.append("%s%s" % (name, ("\n     " + detail) if detail else ""))


# ── summarize_block (pure) ───────────────────────────────────────────────────
r = diag.summarize_block(12.5, ["A", "B", "A", "A"], [])
ok("head names duration and sample count", r.startswith("LOOP BLOCKED 12.5s — 4 sample(s)"), r)
ok("most frequent stack first", r.index("3/4") < r.index("1/4"), r)
ok("no other-threads section when none", "other threads" not in r, r)
r = diag.summarize_block(3.0, ["X"], ["[t] Y", "[t] Y"], ongoing=True)
ok("ongoing is marked", "(still)" in r, r)
ok("other threads listed with counts", "(2x) [t] Y" in r, r)


# ── the sampler names the culprit ────────────────────────────────────────────
def _culprit_blocking_call():
    time.sleep(diag.LOOP_BLOCK_SAMPLE_S + 1.0)


async def _scenario():
    lag = asyncio.create_task(diag._measure_loop_lag())
    await asyncio.sleep(0.6)
    ok("no report before any block", diag.state.last_block == "")
    _culprit_blocking_call()
    # The report lands once the heartbeat is fresh again.
    for _ in range(20):
        await asyncio.sleep(0.2)
        if diag.state.last_block:
            break
    lag.cancel()


asyncio.run(_scenario())
ok("a block was reported", bool(diag.state.last_block))
ok("report names the blocking function", "_culprit_blocking_call" in diag.state.last_block,
   diag.state.last_block)
ok("report shows the blocking line", "time.sleep" in diag.state.last_block,
   diag.state.last_block)


print("%d passed, %d failed" % (_PASS, len(_FAIL)))
for f in _FAIL:
    print("FAIL:", f)
sys.exit(1 if _FAIL else 0)
