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

# ── a request that waits on Jackett by design is not "stuck" ─────────────────
# 2026-10-11: a Test-all waiting on a Cloudflare check wrote a CRITICAL stall
# dump for a server that was idle.
diag.state.inflight.clear()
_now = time.monotonic()
diag.state.inflight[1] = (_now - 80, "POST /api/admin/indexers/test-all")
diag.state.inflight[2] = (_now - 70, "POST /api/admin/indexers/1337x/test")
age, label = diag.oldest_inflight()
ok("a Test in flight for 80 s is not the oldest request", (age, label) == (0.0, ""), repr((age, label)))
diag.state.inflight[3] = (_now - 40, "GET /api/library")
age, label = diag.oldest_inflight()
ok("an ordinary request beside it still is", label == "GET /api/library" and age >= 40, repr((age, label)))
diag.state.inflight[4] = (_now - diag.PATIENT_S - 5, "POST /api/admin/indexers/test-all")
age, label = diag.oldest_inflight()
ok("a Test past its allowance is stuck like any other",
   label.endswith("/test-all") and age >= diag.PATIENT_S, repr((age, label)))
diag.state.inflight.clear()
diag.state.inflight[5] = (_now - 80, "POST /api/admin/indexers/1337x/config")
ok("saving an indexer gets no allowance", diag.oldest_inflight()[1].endswith("/config"))
diag.state.inflight.clear()

# ── child_log: a helper process's own log ────────────────────────────────────
import tempfile                                      # noqa: E402
from pathlib import Path                             # noqa: E402
with tempfile.TemporaryDirectory() as _d:
    got = diag.child_log(Path(_d) / "logs", "flaresolverr")
    ok("the log is logs/<name>.log", got == str(Path(_d) / "logs" / "flaresolverr.log"), got)
    ok("the folder is made", (Path(_d) / "logs").is_dir())
    Path(got).write_bytes(b"x" * 50)
    diag.child_log(Path(_d) / "logs", "flaresolverr", cap=100)
    ok("a small log is left alone", Path(got).stat().st_size == 50)
    diag.child_log(Path(_d) / "logs", "flaresolverr", cap=10)
    ok("a big one is rolled to .1", not Path(got).exists()
       and Path(got + ".1").stat().st_size == 50)
    Path(got).write_bytes(b"y" * 60)
    diag.child_log(Path(_d) / "logs", "flaresolverr", cap=10)
    ok("rolling again replaces the old .1", Path(got + ".1").stat().st_size == 60)


# ── allocated_bytes: a sparse file is long and empty ─────────────────────────
with tempfile.TemporaryDirectory() as _d:
    sp = Path(_d) / "sparse.bin"
    with open(sp, "wb") as fh:
        fh.seek(64 * 1024 * 1024 - 1)
        fh.write(b"\0")
    a = diag.allocated_bytes(sp)
    ok("allocated bytes is a number here", isinstance(a, int), repr(a))
    # A filesystem that cannot hold sparse files allocates it all; that is a
    # fact about the disk, not a failure, so only check the sparse case where
    # the filesystem keeps the hole.
    if a is not None and a < sp.stat().st_size:
        ok("a sparse file allocates less than its length", a < 1024 * 1024, str(a))
    dense = Path(_d) / "dense.bin"
    dense.write_bytes(b"x" * 200_000)
    a = diag.allocated_bytes(dense)
    ok("a written file allocates at least its bytes", a is not None and a >= 200_000, repr(a))
    ok("a missing file is None", diag.allocated_bytes(Path(_d) / "nope") is None)
    ok("file_holders never raises", isinstance(diag.file_holders([dense]), list))


# ── access lines carry the device that sent them ─────────────────────────────
import contextvars                                    # noqa: E402
import logging                                        # noqa: E402

_flt = diag._AccessTagFilter()


def _access_line(tag):
    rec = logging.LogRecord("uvicorn.access", logging.INFO, "", 0,
                            '%s - "%s %s HTTP/%s" %d',
                            ("192.168.0.106:5000", "GET", "/api/x", "1.1", 200), None)

    def run():
        diag.set_access_tag(tag)
        _flt.filter(rec)
    contextvars.Context().run(run)
    return rec.getMessage()


ok("a request with a device id is tagged",
   _access_line("3f2a9c1e-aaaa-bbbb").endswith(' 200 dev=3f2a9c1e'), _access_line("3f2a9c1e-aaaa"))
ok("one without is left alone", _access_line("").endswith(' 200'), _access_line(""))
ok("a percent sign cannot break the line", "dev=ab" in _access_line("a%b"), _access_line("a%b"))


def _scope(*headers):
    return {"type": "http", "headers": [(k.encode(), v.encode()) for k, v in headers]}


ok("device id from the header", diag.scope_device_id(_scope(("x-device-id", "abc-123"))) == "abc-123")
ok("device id from the cookie",
   diag.scope_device_id(_scope(("cookie", "x=1; streamlink_device_id=feed_99; y=2"))) == "feed_99")
ok("the header wins over the cookie",
   diag.scope_device_id(_scope(("cookie", "streamlink_device_id=cookie"),
                               ("x-device-id", "header"))) == "header")
ok("a malformed id is ignored", diag.scope_device_id(_scope(("x-device-id", "a b"))) == "")
ok("no id is ''", diag.scope_device_id(_scope(("cookie", "other=1"))) == "")


print("%d passed, %d failed" % (_PASS, len(_FAIL)))
for f in _FAIL:
    print("FAIL:", f)
sys.exit(1 if _FAIL else 0)
