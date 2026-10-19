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


# ── disk_sample: who else is using the disk (#96) ────────────────────────────
from types import SimpleNamespace as _NS   # noqa: E402


class _Denied(Exception):
    pass


class _FakeProc:
    def __init__(self, pid, name, r, w, denied=False):
        self.pid, self.info, self._io, self._denied = pid, {"name": name}, (r, w), denied

    def io_counters(self):
        if self._denied:
            raise _Denied()
        return _NS(read_bytes=self._io[0], write_bytes=self._io[1])


def _dk(rc, wc, rt, wt, rb, wb):
    return _NS(read_count=rc, write_count=wc, read_time=rt, write_time=wt,
               read_bytes=rb, write_bytes=wb)


class _FakePsutil:
    AccessDenied = _Denied

    def __init__(self):
        self.disks, self.procs = {}, []

    def disk_io_counters(self, perdisk=False):
        return self.disks

    def process_iter(self, attrs=None):
        return iter(self.procs)


_MB = 1_048_576


async def _in_loop(fn):
    return fn()


def _line():
    return asyncio.run(_in_loop(diag.snapshot_line))


def _bad():
    return asyncio.run(_in_loop(lambda: diag._anomalies(diag.snapshot())))
_real_psutil, _real_names, _real_mono = diag.psutil, diag._drive_names, diag.time.monotonic
_clock = [100.0]
fake = _FakePsutil()
diag.psutil = fake
diag._drive_names = lambda: {"PhysicalDrive1": "F:"}
diag.time.monotonic = lambda: _clock[0]
diag._disk_prev.clear()
diag.state.disk_last = {}
try:
    fake.disks = {"PhysicalDrive0": _dk(10, 10, 100, 100, 0, 0),
                  "PhysicalDrive1": _dk(10, 10, 100, 100, 0, 0)}
    fake.procs = [_FakeProc(1, "MsMpEng.exe", 0, 0), _FakeProc(2, "python.exe", 0, 0),
                  _FakeProc(3, "qbittorrent.exe", 0, 0), _FakeProc(4, "svchost.exe", 0, 0),
                  _FakeProc(5, "lsass.exe", 0, 0, denied=True)]
    ok("the first sample only primes the counters", diag.disk_sample() == {})
    ok("so the line has no disk fields yet", " dsk=" not in _line())

    _clock[0] += 30.0
    # F: 100 I/Os taking 85,000 ms (850 ms each) moving 30 MB/s; C: 50 I/Os at 4 ms.
    fake.disks = {"PhysicalDrive0": _dk(35, 35, 200, 200, 3 * _MB, 0),
                  "PhysicalDrive1": _dk(60, 60, 42600, 42600, 900 * _MB, 0)}
    fake.procs = [_FakeProc(1, "MsMpEng.exe", 2550 * _MB, 0),       # 85 MB/s read
                  _FakeProc(2, "python.exe", 3 * _MB, 30 * _MB),      # 1.1 MB/s
                  _FakeProc(3, "qbittorrent.exe", 0, 60 * _MB),       # 2 MB/s write
                  _FakeProc(4, "svchost.exe", 1, 1),                  # under 1 MB/s
                  _FakeProc(5, "lsass.exe", 0, 0, denied=True),
                  _FakeProc(6, "new.exe", 9 * 10**9, 0)]              # no baseline
    d = diag.disk_sample()
    ok("drives sorted slowest first, letters where known",
       [x["disk"] for x in d["disks"]] == ["F:", "PhysicalDrive0"], str(d))
    ok("time per I/O is the delta time over the delta ops",
       abs(d["disks"][0]["ms"] - 850.0) < 1e-6 and abs(d["disks"][1]["ms"] - 4.0) < 1e-6, str(d))
    ok("MB/s over the sample interval", abs(d["disks"][0]["mbs"] - 30.0) < 1e-6, str(d))
    ok("top processes by bytes moved, at most IO_TOP_N",
       [p["name"] for p in d["io_top"]] == ["MsMpEng.exe", "qbittorrent.exe", "python.exe"],
       str(d["io_top"]))
    ok("a process with no baseline is not credited with its lifetime",
       all(p["name"] != "new.exe" for p in d["io_top"]))
    ok("unreadable processes are counted", d["io_denied"] == 1, str(d))
    line = _line()
    ok("the vitals line carries the disks and the top list",
       " dsk=F:850ms/30.0MBps,PhysicalDrive0:4.0ms/0.1MBps io=MsMpEng.exe:85.0r+0.0w," in line
       and line.endswith(" io_denied=1"), line)
    bad = _bad()
    ok("a slow drive is an anomaly that names the busiest process",
       any(b.startswith("disk F: 850 ms per I/O") and "MsMpEng.exe 85.0 MB/s read" in b
           and "(1 processes unreadable)" in b for b in bad), str(bad))
    ok("the fast drive is not", not any("PhysicalDrive0" in b for b in bad), str(bad))

    _clock[0] += 30.0
    # Two slow I/Os are too few to blame the drive; pid 1 now belongs to another program.
    fake.disks = {"PhysicalDrive1": _dk(61, 61, 43600, 43600, 900 * _MB, 0)}
    fake.procs = [_FakeProc(1, "other.exe", 9 * 10**9, 0)]
    d = diag.disk_sample()
    ok("a handful of I/Os never raises the anomaly",
       not any(b.startswith("disk ") for b in _bad()), str(d))
    ok("a reused pid is not credited", d["io_top"] == [], str(d))
    ok("an idle interval reads io=-", " io=- " in _line())

    diag.state.disk_last = {"error": "timeout"}
    ok("a timed-out sample says so instead of repeating the last one",
       _line().endswith(" dsk=timeout"), _line())
    diag._disk_busy.acquire()
    try:
        ok("an overlapping sample is skipped", diag.disk_sample() == {})
    finally:
        diag._disk_busy.release()
    diag.psutil = None
    ok("no psutil: nothing", diag.disk_sample() == {})
finally:
    diag.psutil, diag._drive_names, diag.time.monotonic = _real_psutil, _real_names, _real_mono
    diag.state.disk_last = {}
    diag._disk_prev.clear()

# The drive-letter lookup must run on the first sample even when the monotonic
# clock is small: Windows counts it from boot (20.25.3 never looked it up in the
# first 10 minutes after a reboot).
_real_win, _real_plat, _real_mono2 = diag._windows_drive_map, diag.sys.platform, diag.time.monotonic
_looked = []
diag._windows_drive_map = lambda: (_looked.append(1), {"PhysicalDrive2": "F:"})[1]
diag.sys.platform = "win32"
diag.time.monotonic = lambda: 90.0          # 90 s after boot
diag._drive_map.update(t=None, map={})
try:
    got = diag._drive_names()
    ok("letters are looked up on the first sample, however soon after boot",
       got == {"PhysicalDrive2": "F:"} and _looked == [1], str(got))
    diag._drive_names()
    ok("and not again within the refresh interval", _looked == [1])
finally:
    diag._windows_drive_map, diag.sys.platform, diag.time.monotonic = _real_win, _real_plat, _real_mono2
    diag._drive_map.update(t=None, map={})
ok("sub-10 ms latency keeps a decimal", diag._ms(0.43) == "0.4" and diag._ms(850.2) == "850")

ok("Linux partitions and loop devices are skipped, disks kept",
   [n for n in ("loop0", "ram1", "sda", "nvme0n1") if not diag._skip_disk(n)]
   == (["sda", "nvme0n1"]))


print("%d passed, %d failed" % (_PASS, len(_FAIL)))
for f in _FAIL:
    print("FAIL:", f)
sys.exit(1 if _FAIL else 0)
