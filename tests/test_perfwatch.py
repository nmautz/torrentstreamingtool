"""Interactive requests are timed, and a slow one says where its time went (20.26.11, #104).

    python tests/test_perfwatch.py      (or `make test`)
"""
import asyncio, logging, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import perfwatch                          # noqa: E402
import diagnostics as diag                # noqa: E402

fails = []


def check(name, ok, detail=""):
    print(("  ok    " if ok else "  FAIL  ") + name + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        fails.append(name)


print("which requests are judged")
for tmpl, want in (("/api/library/{item_id}/files", "ui"), ("/api/library/coverage", "ui"),
                   ("/api/admin/storage-breakdown", "ui"), ("/api/events", "stream"),
                   ("/api/library/offline-cache/{cache_key}/{filename}", "media"),
                   ("/StreamLinkBundles/{cache_key}/{filename}", "media"),
                   ("/api/library/ondemand/{session_key}/{filename}", "media"),
                   ("/api/metadata/img/{size}/{filename}", "media"), ("(static)", "media"),
                   ("/api/diag/client-log", "upload"), ("/api/search", "external"),
                   ("/api/library/{item_id}/bundle-manifest", "background"),
                   ("/api/admin/logs/{name}", "bulk"), ("/api/admin/logsx", "ui"),
                   ("/healthz", "probe"), ("/api/ambient/weather", "external")):
    check(f"{tmpl} is {want}", perfwatch.classify("GET", tmpl) == want,
          perfwatch.classify("GET", tmpl))

print("the summary")
st = perfwatch.Stats()
for ms in range(1, 101):
    st.record("ui", "GET", "/api/library/coverage", ms / 1000)
st.record("ui", "GET", "/api/profiles", 0.9)
st.record("media", "GET", "/api/metadata/img/{size}/{filename}", 3.0)
rows = st.drain()
cov = next(r for r in rows if r["route"] == "GET /api/library/coverage")
check("p50 / p95 / max per route", (round(cov["p50"], 3), round(cov["p95"], 3), cov["max"])
      == (0.051, 0.095, 0.1), str(cov))
check("slowest p95 first", rows[0]["route"] == "GET /api/metadata/img/{size}/{filename}")
line = perfwatch.summary_line(rows, 5)
check("the line lists interactive routes and totals the rest",
      line.startswith("REQ STATS 5m ui n=101: GET /api/profiles n=1 p50=900ms")
      and "| media n=1 max=3000ms" in line, line)
check("draining empties it", st.drain() == [] and perfwatch.summary_line([], 5) is None)
big = perfwatch.Stats()
for i in range(3 * perfwatch.SAMPLES_PER_ROUTE):
    big.record("ui", "GET", "/x", 0.001)
r = big.drain()[0]
check("samples are bounded, the count is not",
      r["n"] == 3 * perfwatch.SAMPLES_PER_ROUTE)

print("where a slow request's time went")
seen = []


class _Grab(logging.Handler):
    def emit(self, record):
        seen.append(record.getMessage())


diag.log.addHandler(_Grab())
diag.log.setLevel(logging.INFO)


async def scenario():
    loop = asyncio.get_running_loop()
    pool = diag.TimedExecutor(max_workers=1)
    loop.set_default_executor(pool)
    lock = diag.InstrumentedLock(asyncio.Lock(), "test-library")

    async def holder():
        async with lock:
            await asyncio.sleep(0.3)

    async def request():
        cost = diag.cost_begin()
        diag.note_request_start(1, "GET", "/api/library/x/files")
        await asyncio.sleep(0.01)
        async with lock:                                # waits ~0.3 s for holder
            pass
        busy = loop.run_in_executor(None, time.sleep, 0.2)   # pool's only worker
        await asyncio.to_thread(lambda: None)               # queued behind it ~0.2 s
        await busy
        diag.note_request_end(1, 200, "GET", "/api/library/{item_id}/files", cost)
        return cost

    h = asyncio.ensure_future(holder())
    await asyncio.sleep(0)
    cost = await request()
    await h
    return cost


cost = asyncio.run(scenario())
check("lock wait is charged to the request", 0.25 <= cost["lock"] <= 0.5, str(cost))
check("so is the wait for a worker thread", 0.12 <= cost["pool"] <= 0.4, str(cost))
slow = [m for m in seen if m.startswith("SLOW UI ")]
check("a SLOW UI line names the route and the split",
      len(slow) == 1 and " GET /api/library/{item_id}/files status=200 lock=0." in slow[0]
      and " pool=0." in slow[0] and " other=" in slow[0], str(slow))
seen.clear()
diag.note_request_start(2, "GET", "/api/library/offline-cache/k/seg.m4s")
time.sleep(0.6)
diag.note_request_end(2, 200, "GET", "/api/library/offline-cache/{cache_key}/{filename}", None)
check("a slow video segment is counted, not judged", not any(m.startswith("SLOW UI") for m in seen))
diag.cost_add("lock", 5.0)                              # no request: a no-op
check("outside a request nothing is charged", diag._req_cost.get() is None)

print("background waits, lifted from main.py")
import ast, types                         # noqa: E402
from datetime import datetime, timedelta, timezone   # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _backend_src                       # noqa: E402
src = _backend_src.read()
tree = ast.parse(src)
WANT = {"_note_download_time", "_phone_dl_begin", "_phone_dl_fetch"}
pieces = [ast.get_source_segment(src, n) for n in tree.body
          if isinstance(n, ast.FunctionDef) and n.name in WANT]
assert len(pieces) == 3
lines = []
fake_log = types.SimpleNamespace(info=lambda m, *a: lines.append(m % a))
ns = {"Path": Path, "time": time, "datetime": datetime, "timezone": timezone,
      "VIDEO_EXTS": {".mkv"}, "log": fake_log, "_DL_TIMES": {}, "_PHONE_DL": {},
      "PHONE_DL_TTL_S": 6 * 3600,
      "_parse_iso_dt": lambda v: datetime.fromisoformat(v) if v else None}
exec("\n\n".join(pieces), ns)
added = (datetime.now(timezone.utc) - timedelta(seconds=300)).isoformat()
item = {"id": "ds", "title": "Demon Slayer", "added_at": added}
q = [{"name": "e1.mkv", "progress": 0.4, "priority": 1}, {"name": "e2.mkv", "progress": 0, "priority": 1}]
ns["_note_download_time"](item, q, False)
check("the first look only notes the item", lines == [])
q[0]["progress"] = 1.0
ns["_note_download_time"](item, q, False)
check("the first playable episode is timed from added_at",
      len(lines) == 1 and lines[0].startswith("DOWNLOAD TIME Demon Slayer: first episode playable after 30"),
      str(lines))
q[1]["progress"] = 1.0
ns["_note_download_time"](item, q, True)
ns["_note_download_time"](item, q, True)
check("and the whole download once", len(lines) == 2 and "all 2 file(s) done" in lines[1], str(lines))
old_item = {"id": "old", "title": "Old", "added_at": added}
ns["_note_download_time"](old_item, [{"name": "a.mkv", "progress": 1.0, "priority": 1}], False)
ns["_note_download_time"](old_item, [{"name": "a.mkv", "progress": 1.0, "priority": 1}], True)
check("an item already part-done when first seen gets no first-playable time",
      not any("Old: first" in x for x in lines), str(lines))
lines.clear()
ns["_phone_dl_begin"]("k1", 3, 30_000_000, "dev1", "ep.mkv")
ns["_phone_dl_begin"]("k1", 3, 30_000_000, "dev1", "ep.mkv")     # the app retrying
for f in ("a", "b", "b", "c"):
    ns["_phone_dl_fetch"]("k1", f, "dev1")
check("a phone download is logged once its last distinct file is served",
      len(lines) == 1 and lines[0].startswith("PHONE DOWNLOAD ep.mkv: 3 files, 30 MB in ")
      and "dev=dev1" in lines[0] and ns["_PHONE_DL"] == {}, str(lines))
lines.clear()
ns["_phone_dl_begin"]("k2", 2, 1, "dev1", "x.mkv")
ns["_phone_dl_fetch"]("k2", "a", "")
ns["_phone_dl_fetch"]("k2", "b", "")
check("a downloader that names no device still matches the one download of that bundle",
      len(lines) == 1, str(lines))
ns["_phone_dl_fetch"]("nope", "a", "dev1")
check("a bundle nobody asked a manifest for is ignored", len(lines) == 1)

print()
if fails:
    print(f"{len(fails)} FAILED"); sys.exit(1)
print("all passed")
