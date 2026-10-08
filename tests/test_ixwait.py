"""Unit tests for `ixwait.py` - how long a search waits for each indexer.

    python tests/test_ixwait.py      (or `make test`)

No Jackett. The timings are the ones measured on the box on 2026-10-08.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ixwait as iw          # noqa: E402

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


def near(name, got, want):
    ok(name, abs(got - want) < 1e-6, "got %r, want %r" % (got, want))


# ── Pace ──────────────────────────────────────────────────────────────────────
p = iw.Pace()
ok("unknown indexer is not slow", not p.is_slow("1337x"))
eq("unknown indexer has no last", p.last("1337x"), None)
p.note("eztv", 0.5)
ok("a quick answer is not slow", not p.is_slow("eztv"))
p.note("1337x", 11.4)
ok("one late answer marks it slow", p.is_slow("1337x"))
for _ in range(iw.WINDOW - 1):
    p.note("1337x", 0.05)           # Jackett's cache
ok("cache hits in between do not clear it", p.is_slow("1337x"))
p.note("1337x", 0.05)
ok("a full window of quick answers clears it", not p.is_slow("1337x"))
p.note("eztv", 44.8)                # the first solve
ok("EZTV's first solve marks it slow", p.is_slow("eztv"))
near("last", p.last("eztv"), 44.8)
p.note("x", iw.SLOW)
ok("exactly SLOW counts as slow", p.is_slow("x"))

# ── more_wait ─────────────────────────────────────────────────────────────────
near("nothing pending answers now", iw.more_wait(0.3, 0, 0, 0.3, True), 0.0)
near("quick ones pending wait to SOFT", iw.more_wait(2.0, 1, 1, None, True), iw.SOFT - 2.0)
near("slow one gets GRACE after the quick ones",
     iw.more_wait(1.0, 0, 1, 1.0, True), iw.GRACE)
near("GRACE spent", iw.more_wait(2.5, 0, 1, 1.0, True), 0.0)
near("GRACE never runs past SOFT", iw.more_wait(11.8, 0, 1, 11.5, True), iw.SOFT - 11.8)
near("nothing to show: keep waiting to SOFT",
     iw.more_wait(1.0, 0, 1, 1.0, False), iw.SOFT - 1.0)
near("nothing to show and SOFT passed", iw.more_wait(12.5, 0, 1, 1.0, False), 0.0)
near("only slow indexers: wait them out",
     iw.more_wait(13.0, 0, 1, None, False), iw.HARD - 13.0)
near("only slow indexers, one already in: still wait",
     iw.more_wait(13.0, 0, 1, None, True), iw.HARD - 13.0)
near("patient waits to HARD", iw.more_wait(20.0, 0, 1, 1.0, True, patient=True),
     iw.HARD - 20.0)
near("patient with nothing pending", iw.more_wait(20.0, 0, 0, 1.0, True, patient=True), 0.0)
near("never negative", iw.more_wait(500.0, 1, 1, None, True), 0.0)

# The box, 2026-10-08: six quick indexers in by 0.95 s, 1337x at 11.5 s.
near("the measured search answers at 1.95 s",
     0.95 + iw.more_wait(0.95, 0, 1, 0.95, True), 1.95)

# ── failure_text ──────────────────────────────────────────────────────────────
class ReadTimeout(Exception):
    pass


ok("a timeout has a reason", "no answer" in iw.failure_text(ReadTimeout("")))
eq("an error keeps its message", iw.failure_text(ValueError("bad json")), "bad json")
eq("an error with no message gets its name", iw.failure_text(KeyError()), "KeyError")
ok("long messages are cut", len(iw.failure_text(ValueError("x" * 900))) == 200)

# ── jackett_error ─────────────────────────────────────────────────────────────
TRACE = """Jackett.Common.IndexerException: Exception (torrentproject2): Received an unexpected EOF or 0 bytes from the transport stream.
 ---> System.Net.Http.HttpRequestException: The SSL connection could not be established, see inner exception.
 ---> System.IO.IOException: Received an unexpected EOF or 0 bytes from the transport stream.
   at System.Net.Security.SslStream.ReceiveHandshakeFrameAsync[TIOAdapter](CancellationToken cancellationToken)
   at Jackett.Server.Controllers.ResultsController.Results(ApiSearch requestt)"""
got = iw.jackett_error(TRACE)
ok("a stack trace becomes one line", "\n" not in got and " at System" not in got, got)
ok("it keeps what failed", "torrentproject2" in got, got)
ok("it keeps the readable cause", "SSL connection could not be established" in got, got)
eq("no error stays empty", iw.jackett_error(""), "")
eq("None stays empty", iw.jackett_error(None), "")
eq("a plain message is kept", iw.jackett_error("Challenge detected"), "Challenge detected")
ok("capped", len(iw.jackett_error("x" * 900)) == 300)

ok("constants are ordered", iw.GRACE < iw.SLOW < iw.SOFT < iw.HARD <= iw.ADMIN)

print("ixwait: %d passed, %d failed" % (_PASS, len(_FAIL)))
for f in _FAIL:
    print("  FAIL " + f)
sys.exit(1 if _FAIL else 0)
